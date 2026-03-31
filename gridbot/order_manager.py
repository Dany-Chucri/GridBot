"""OrderManager — the ONLY module that talks to the exchange for order ops.

Responsibilities (design doc section 3.2):
- Compute diffs between desired orders and actual open orders (section 7.2)
- Submit all changes as a single batch request (cancels + placements)
- Handle Post-Only (ALO) rejections with bounded retry (section 7.4)
- Use deterministic client order IDs for idempotency (section 7.3)
- Manage server-side backstop stop-losses (section 6.8)
- Execute emergency flatten protocol (section 6.7)

CRITICAL: All order operations MUST be batched. Never send individual order calls.
"""

from __future__ import annotations

import logging

from gridbot.config import AssetConfig, BotConfig
from gridbot.types import (
    DesiredOrder,
    Fill,
    OpenOrder,
    OrderSide,
    Position,
)

logger = logging.getLogger(__name__)


class OrderManager:
    """Manages all exchange order operations via batch API calls."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._client = None  # Hyperliquid SDK client, set in initialize()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the exchange SDK client."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Reconciliation: diff and batch (section 7.2)
    # ------------------------------------------------------------------

    async def reconcile(
        self,
        symbol: str,
        desired: list[DesiredOrder],
        current: list[OpenOrder],
    ) -> None:
        """Compute minimal diff and submit as single batch request.

        1. Identify orders to cancel (on exchange but not in desired set)
        2. Identify orders to place (in desired set but not on exchange)
        3. No-op for matching orders (same price, side, size, flags)
        4. Submit cancels + placements as ONE batch request
        """
        raise NotImplementedError

    def _compute_diff(
        self,
        desired: list[DesiredOrder],
        current: list[OpenOrder],
    ) -> tuple[list[OpenOrder], list[DesiredOrder]]:
        """Compute (to_cancel, to_place) diff.

        Matching criteria: same price, side, size, and flags.
        Only touch orders that actually need to change (section 7.2).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def _submit_batch(
        self,
        cancels: list[OpenOrder],
        placements: list[DesiredOrder],
    ) -> None:
        """Submit cancel + place as a single atomic batch request.

        This is the ONLY method that sends order operations to the exchange.
        """
        raise NotImplementedError

    async def cancel_all_orders(self, symbol: str) -> None:
        """Cancel all resting orders for an asset (batch cancel)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Post-Only rejection handling (section 7.4)
    # ------------------------------------------------------------------

    async def _handle_alo_rejection(
        self,
        order: DesiredOrder,
        mid_price: float,
        attempt: int,
    ) -> DesiredOrder | None:
        """Nudge limit price one tick farther from mid and retry.

        Max retries: post_only_max_retries per level per cycle.
        Returns adjusted order, or None if max retries exceeded.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Fill handling & grid flip (section 7.5)
    # ------------------------------------------------------------------

    def compute_flip_order(
        self,
        fill: Fill,
        step_bps: float,
        inventory_zone_is_hard_cap: bool,
    ) -> DesiredOrder | None:
        """Compute the opposite-side flip order for a full fill.

        If within inventory caps: place opposite side one step away.
        If at hard cap: reduce-only order to unwind. No flip.
        Returns None if partial fill (don't flip on partials).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Server-side backstop stop-loss (section 6.8)
    # ------------------------------------------------------------------

    async def update_backstop(
        self,
        symbol: str,
        position: Position,
        anchor: float,
        atr: float,
        breakout_atr_distance: float,
        backstop_buffer_atr: float,
    ) -> None:
        """Maintain server-side trigger stop-loss for dead-man's switch.

        - Direction: opposite to current position
        - Size: full current position size
        - Trigger: anchor +/- (breakout_atr_distance + backstop_buffer_atr) * ATR
        - Type: trigger market, reduce-only, tpsl="sl"

        Cancel and replace on every position change.
        Remove when position reaches zero.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Emergency flatten protocol (section 6.7)
    # ------------------------------------------------------------------

    async def execute_flatten(
        self,
        symbol: str,
        position: Position,
        config: AssetConfig,
        get_mid_price: callable,
        get_book_depth: callable,
        get_position: callable,
    ) -> bool:
        """Execute the emergency flatten state machine.

        Steps:
        1. Pre-flatten depth assessment
        2. IOC with bounded slippage (reduce-only)
        3. Partial fill retry loop with time budget
        4. Slippage escalation on failure
        5. Dead state if still can't flatten

        Returns True if fully flattened, False if residual remains.
        """
        raise NotImplementedError

    async def _send_flatten_ioc(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        limit_price: float,
    ) -> None:
        """Send a single IOC reduce-only order for flattening."""
        raise NotImplementedError
