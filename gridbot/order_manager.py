"""OrderManager, the ONLY module that talks to the exchange for order ops.

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

import asyncio
import hashlib
import logging
import math
import os
import time
from typing import Any, Callable

from gridbot.config import AssetConfig, BotConfig
from gridbot.types import (
    DesiredOrder,
    Fill,
    OpenOrder,
    OrderSide,
    Position,
    TimeInForce,
)

logger = logging.getLogger(__name__)


class OrderManager:
    """Manages all exchange order operations via batch API calls."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._client = None  # Hyperliquid Exchange client, set in initialize()
        self._info = None  # Hyperliquid Info client, set in initialize()
        self._wallet_address: str = ""
        # ALO rejection tracking: symbol -> count in current window
        self._alo_rejection_counts: dict[str, int] = {}
        self._alo_rejection_window_start_ms: int = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_coin(symbol: str) -> str:
        """'BTC-PERP' -> 'BTC'."""
        return symbol.replace("-PERP", "").upper()

    @staticmethod
    def _generate_order_id(
        symbol: str,
        price: float,
        side: OrderSide,
        config_hash: str,
        epoch: int,
    ) -> str:
        """Deterministic client order ID (design doc section 7.3).

        Returns a 32-char hex string (16 bytes) prefixed with '0x' for Cloid.
        """
        raw = f"{symbol}|{price:.8f}|{side.value}|{config_hash}|{epoch}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"0x{digest[:32]}"

    @staticmethod
    def _generate_backstop_id(
        symbol: str,
        direction: str,
        config_hash: str,
    ) -> str:
        """Deterministic backstop order ID (design doc section 6.8)."""
        raw = f"backstop|{symbol}|{direction}|{config_hash}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"0x{digest[:32]}"

    @staticmethod
    def _tif_to_sdk(tif: TimeInForce) -> str:
        """Convert our TimeInForce enum to Hyperliquid SDK string."""
        return {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}[tif.value]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the exchange SDK client.

        Reads wallet private key from GRIDBOT_PRIVATE_KEY env var. The key may
        belong to an API/agent wallet (Hyperliquid's delegated-signer model);
        in that case `config.wallet_address` names the master trading account
        and is passed as `account_address` so the SDK signs with the agent key
        but reads/writes state on the master account, matching MarketData's
        existing use of `config.wallet_address` for the same account.
        Validates connectivity with an account state query.
        """
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info

        private_key = os.environ.get("GRIDBOT_PRIVATE_KEY", "")
        if not private_key:
            raise RuntimeError(
                "GRIDBOT_PRIVATE_KEY environment variable not set"
            )

        wallet = Account.from_key(private_key)
        self._wallet_address = self._config.wallet_address or wallet.address

        base_url = self._config.base_url
        self._info = Info(base_url, skip_ws=True)
        self._client = Exchange(
            wallet=wallet,
            base_url=base_url,
            account_address=self._config.wallet_address or None,
        )

        # Validate connectivity
        loop = asyncio.get_running_loop()
        state = await loop.run_in_executor(
            None, self._info.user_state, self._wallet_address
        )
        equity = float(state.get("marginSummary", {}).get("accountValue", 0))
        logger.info(
            "OrderManager initialized, wallet=%s equity=%.2f testnet=%s",
            self._wallet_address[:10] + "...",
            equity,
            self._config.testnet,
        )

    # ------------------------------------------------------------------
    # Reconciliation: diff and batch (section 7.2)
    # ------------------------------------------------------------------

    def _get_asset_config(self, symbol: str) -> AssetConfig:
        """Resolve AssetConfig for a given symbol."""
        for asset in self._config.assets:
            if asset.symbol == symbol:
                return asset
        return self._config.assets[0]

    async def reconcile(
        self,
        symbol: str,
        desired: list[DesiredOrder],
        current: list[OpenOrder],
        mid_price: float,
    ) -> None:
        """Compute minimal diff and submit as single batch request.

        1. Identify orders to cancel (on exchange but not in desired set)
        2. Identify orders to place (in desired set but not on exchange)
        3. No-op for matching orders (same price, side, size, flags)
        4. Submit cancels + placements as batch requests

        Args:
            mid_price: Current mid price, used for ALO rejection nudging.
                Must be positive; ALO retries are suppressed otherwise.
        """
        if mid_price <= 0 and desired:
            logger.warning(
                "Reconcile %s: mid_price=%.2f, ALO retries will be suppressed",
                symbol, mid_price,
            )

        to_cancel, to_place = self._compute_diff(desired, current)

        if not to_cancel and not to_place:
            return

        logger.info(
            "Reconcile %s: cancel=%d place=%d (desired=%d current=%d)",
            symbol,
            len(to_cancel),
            len(to_place),
            len(desired),
            len(current),
        )

        await self._submit_batch(to_cancel, to_place, symbol, mid_price)

    async def reconcile_with_backstop(
        self,
        symbol: str,
        desired: list[DesiredOrder],
        current: list[OpenOrder],
        mid_price: float,
        position: Position,
        anchor: float,
        atr: float,
        breakout_atr_distance: float,
        backstop_buffer_atr: float,
        config_hash: str = "",
    ) -> None:
        """Reconcile grid orders AND update backstop in the same batch.

        Minimizes the window where the backstop is absent by including
        backstop cancel/place in the same cancel/place sequence as grid orders.
        """
        to_cancel, to_place = self._compute_diff(desired, current)
        pos_size = position.size if position else 0.0
        coin = self._to_coin(symbol)

        extra_cancels: list[dict] = []
        extra_places: list[dict] = []

        # Build backstop cancel/place
        if abs(pos_size) < 1e-12:
            # Cancel backstops for both directions
            for d in ("long", "short"):
                cloid = self._generate_backstop_id(symbol, d, config_hash)
                cancel_oids = await self._find_backstop_oids(symbol, cloid)
                extra_cancels.extend(cancel_oids)
        else:
            direction = "long" if pos_size > 0 else "short"
            is_buy = pos_size < 0
            distance = (breakout_atr_distance + backstop_buffer_atr) * atr
            trigger_price = (anchor - distance) if pos_size > 0 else (anchor + distance)
            backstop_cloid = self._generate_backstop_id(symbol, direction, config_hash)

            cancel_oids = await self._find_backstop_oids(symbol, backstop_cloid)
            extra_cancels.extend(cancel_oids)

            # Also cancel the opposite direction's backstop in case the
            # position flipped sign since the last update, otherwise that
            # stale, differently-cloid'd trigger is orphaned on the exchange.
            other_direction = "short" if direction == "long" else "long"
            other_cloid = self._generate_backstop_id(symbol, other_direction, config_hash)
            other_cancel_oids = await self._find_backstop_oids(symbol, other_cloid)
            extra_cancels.extend(other_cancel_oids)

            from hyperliquid.utils.types import Cloid

            extra_places.append({
                "coin": coin,
                "is_buy": is_buy,
                "sz": abs(pos_size),
                "limit_px": trigger_price,
                "order_type": {
                    "trigger": {
                        "triggerPx": trigger_price,
                        "isMarket": True,
                        "tpsl": "sl",
                    }
                },
                "reduce_only": True,
                "cloid": Cloid.from_str(backstop_cloid),
            })

        if not to_cancel and not to_place and not extra_cancels and not extra_places:
            return

        logger.info(
            "Reconcile+backstop %s: cancel=%d place=%d backstop_cancel=%d backstop_place=%d",
            symbol,
            len(to_cancel),
            len(to_place),
            len(extra_cancels),
            len(extra_places),
        )

        await self._submit_batch(
            to_cancel, to_place, symbol, mid_price,
            extra_cancel_oids=extra_cancels,
            extra_place_requests=extra_places,
        )

    async def _find_backstop_oids(
        self, symbol: str, expected_cloid: str
    ) -> list[dict]:
        """Find cancel requests for backstop orders matching the expected cloid."""
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)
        try:
            all_open = await loop.run_in_executor(
                None,
                self._info.frontend_open_orders,
                self._wallet_address,
            )
            return [
                {"coin": coin, "oid": int(o["oid"])}
                for o in all_open
                if o.get("coin") == coin and o.get("cloid", "") == expected_cloid
            ]
        except Exception:
            logger.exception("Failed to query backstop orders for %s", symbol)
            return []

    def _compute_diff(
        self,
        desired: list[DesiredOrder],
        current: list[OpenOrder],
    ) -> tuple[list[OpenOrder], list[DesiredOrder]]:
        """Compute (to_cancel, to_place) diff.

        Matching criteria: same client_order_id OR (same price, side, size,
        and reduce_only flag). Only touch orders that actually need to change
        (section 7.2).
        """
        # Build lookup of current orders by client_order_id
        current_by_cloid: dict[str, OpenOrder] = {}
        for order in current:
            if order.client_order_id:
                current_by_cloid[order.client_order_id] = order

        # Build a signature-based lookup for matching without cloid.
        # Use a list per signature to handle multiple orders at the same
        # (price, side, size, reduce_only), e.g. pending flips at the
        # same price.
        def _sig(price: float, side: OrderSide, size: float, reduce_only: bool) -> str:
            return f"{price:.8f}|{side.value}|{size:.8f}|{reduce_only}"

        current_by_sig: dict[str, list[OpenOrder]] = {}
        for order in current:
            sig = _sig(order.price, order.side, order.size, order.reduce_only)
            current_by_sig.setdefault(sig, []).append(order)

        matched_current_ids: set[int] = set()
        to_place: list[DesiredOrder] = []

        for desired_order in desired:
            matched = False

            # Try matching by client_order_id first
            if desired_order.client_order_id in current_by_cloid:
                curr = current_by_cloid[desired_order.client_order_id]
                # Verify the order params still match
                if (
                    math.isclose(curr.price, desired_order.price, rel_tol=1e-6)
                    and curr.side == desired_order.side
                    and math.isclose(curr.size, desired_order.size, rel_tol=1e-6)
                    and curr.reduce_only == desired_order.reduce_only
                ):
                    matched_current_ids.add(curr.order_id)
                    matched = True

            # Fallback: match by signature (price, side, size, reduce_only)
            if not matched:
                sig = _sig(
                    desired_order.price,
                    desired_order.side,
                    desired_order.size,
                    desired_order.reduce_only,
                )
                candidates = current_by_sig.get(sig, [])
                for curr in candidates:
                    if curr.order_id not in matched_current_ids:
                        matched_current_ids.add(curr.order_id)
                        matched = True
                        break

            if not matched:
                to_place.append(desired_order)

        # Orders on exchange with no match -> cancel
        to_cancel = [o for o in current if o.order_id not in matched_current_ids]

        return to_cancel, to_place

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def _submit_batch(
        self,
        cancels: list[OpenOrder],
        placements: list[DesiredOrder],
        symbol: str = "",
        mid_price: float = 0.0,
        extra_cancel_oids: list[dict] | None = None,
        extra_place_requests: list[dict] | None = None,
    ) -> None:
        """Submit cancel + place as batch requests.

        Hyperliquid batch operations are NOT transactional, individual orders
        within a batch can fail independently. The implementation handles
        partially-applied batch state.

        Per SDK constraints, cancels and placements are separate API calls.
        Cancels execute first to free order slots.

        Args:
            extra_cancel_oids: Additional raw cancel dicts (e.g., backstop)
                to include in the cancel batch.
            extra_place_requests: Additional raw placement dicts (e.g., backstop)
                to append after grid placements.
        """
        loop = asyncio.get_running_loop()

        # Step 1: Batch cancel (grid orders + extras like backstop)
        cancel_requests = [
            {"coin": self._to_coin(order.symbol), "oid": order.order_id}
            for order in cancels
        ]
        if extra_cancel_oids:
            cancel_requests.extend(extra_cancel_oids)

        if cancel_requests:
            try:
                result = await loop.run_in_executor(
                    None, self._client.bulk_cancel, cancel_requests
                )
                self._parse_cancel_result(result, cancels)
            except Exception:
                logger.exception(
                    "Batch cancel failed (%d orders)", len(cancel_requests)
                )

        # Step 2: Batch place with ALO retry loop
        all_placements = list(placements)
        extra_raw = extra_place_requests or []

        if all_placements or extra_raw:
            pending = all_placements
            max_attempts = max(
                (self._get_asset_config(o.symbol).post_only_max_retries for o in all_placements),
                default=3,
            ) if all_placements else 0

            for attempt in range(max_attempts + 1):
                order_requests = [self._build_place_request(o) for o in pending]
                # Append extra raw requests only on the first attempt
                if attempt == 0 and extra_raw:
                    order_requests.extend(extra_raw)
                if not order_requests:
                    break
                try:
                    result = await loop.run_in_executor(
                        None, self._client.bulk_orders, order_requests
                    )
                    alo_retries = await self._parse_place_result(
                        result, pending, mid_price, attempt=attempt,
                        extra_requests=extra_raw if attempt == 0 else None,
                    )
                    if not alo_retries:
                        break
                    pending = alo_retries
                except Exception:
                    logger.exception(
                        "Batch place failed (%d orders, attempt %d)",
                        len(order_requests),
                        attempt,
                    )
                    break

    def _build_place_request(self, order: DesiredOrder) -> dict[str, Any]:
        """Build a single Hyperliquid placement request dict."""
        from hyperliquid.utils.types import Cloid

        req: dict[str, Any] = {
            "coin": self._to_coin(order.symbol),
            "is_buy": order.side == OrderSide.BUY,
            "sz": order.size,
            "limit_px": order.price,
            "order_type": self._build_order_type(order),
            "reduce_only": order.reduce_only,
        }
        if order.client_order_id:
            req["cloid"] = Cloid.from_str(order.client_order_id)
        return req

    def _build_order_type(self, order: DesiredOrder) -> dict:
        """Build Hyperliquid order_type dict from a DesiredOrder."""
        return {"limit": {"tif": self._tif_to_sdk(order.time_in_force)}}

    @staticmethod
    def _check_bulk_result(result: Any, label: str, symbol: str) -> bool:
        """Check a bulk_orders/bulk_cancel result for top-level errors.

        Returns True if status is "ok", False (with error log) otherwise.
        """
        if not isinstance(result, dict):
            logger.warning("Unexpected %s result type for %s: %s", label, symbol, type(result))
            return False
        status = result.get("status", "")
        if status == "err":
            logger.error(
                "%s failed for %s: %s", label.capitalize(), symbol, result.get("response", "")
            )
            return False
        return True

    def _parse_cancel_result(
        self, result: Any, cancels: list[OpenOrder]
    ) -> None:
        """Parse cancel batch result and log per-order outcomes."""
        if not isinstance(result, dict):
            logger.warning("Unexpected cancel result type: %s", type(result))
            return

        status = result.get("status", "")
        if status == "ok":
            response = result.get("response", {})
            if response.get("type") == "cancel":
                statuses = response.get("data", {}).get("statuses", [])
                success = sum(1 for s in statuses if "success" in str(s).lower())
                failed = len(statuses) - success
                for i, s in enumerate(statuses):
                    if i >= len(cancels):
                        break
                    order = cancels[i]
                    if "success" in str(s).lower():
                        logger.info(
                            "Cancelled oid=%d %s %s @ %.2f",
                            order.order_id,
                            order.symbol,
                            order.side.value,
                            order.price,
                        )
                    else:
                        logger.warning(
                            "Cancel failed for oid=%d: %s",
                            order.order_id,
                            s,
                        )
                if failed:
                    logger.warning(
                        "Cancel batch: %d succeeded, %d failed", success, failed
                    )
        elif status == "err":
            logger.error("Cancel batch error: %s", result.get("response", ""))

    @staticmethod
    def _is_alo_rejection(status_entry: Any) -> bool:
        """Check if a per-order status indicates an ALO (Post-Only) rejection."""
        if isinstance(status_entry, dict):
            error = status_entry.get("error", "")
            return "BadAloPx" in error or "would have been filled" in error.lower()
        s = str(status_entry)
        return "BadAloPx" in s

    async def _parse_place_result(
        self,
        result: Any,
        placements: list[DesiredOrder],
        mid_price: float = 0.0,
        attempt: int = 0,
        extra_requests: list[dict] | None = None,
    ) -> list[DesiredOrder]:
        """Parse placement batch result, handle ALO rejections.

        `extra_requests` are non-grid raw placement dicts appended after
        `placements` in the same batch (currently: the backstop trigger
        order from reconcile_with_backstop/update_backstop). Their status
        entries sit at statuses[len(placements):] and must be checked too -
        otherwise a rejected backstop leg is silently invisible even though
        the overall batch status reads "ok".

        Returns a list of nudged orders to retry (from ALO rejections).
        """
        retries: list[DesiredOrder] = []
        extra_requests = extra_requests or []

        if not isinstance(result, dict):
            logger.warning("Unexpected place result type: %s", type(result))
            return retries

        status = result.get("status", "")
        if status == "ok":
            response = result.get("response", {})
            if response.get("type") == "order":
                statuses = response.get("data", {}).get("statuses", [])
                for i, s in enumerate(statuses):
                    if i >= len(placements):
                        break
                    order = placements[i]
                    if self._is_order_success(s):
                        logger.info(
                            "Placed %s %s %.6f @ %.2f%s",
                            order.symbol,
                            order.side.value,
                            order.size,
                            order.price,
                            " (reduce-only)" if order.reduce_only else "",
                        )
                        continue
                    if self._is_alo_rejection(s):
                        self._track_alo_rejection(order.symbol)
                        logger.info(
                            "ALO rejection for %s %s @ %.2f (attempt %d)",
                            order.symbol,
                            order.side.value,
                            order.price,
                            attempt,
                        )
                        if mid_price > 0:
                            nudged = await self._handle_alo_rejection(
                                order, mid_price, attempt=attempt
                            )
                            if nudged is not None:
                                retries.append(nudged)
                    else:
                        logger.warning(
                            "Placement failed for %s %s @ %.2f: %s",
                            order.symbol,
                            order.side.value,
                            order.price,
                            s,
                        )

                # Check extra (backstop) statuses, which sit after the grid
                # placements' statuses in the same response array. Design doc
                # section 10.3 rates "backstop order update failed" as a
                # Warning-severity alert (not Critical, the bot's own
                # breakout/flatten logic is still the primary defense; the
                # backstop is a fallback for when that fails too).
                for j, extra in enumerate(extra_requests):
                    idx = len(placements) + j
                    if idx >= len(statuses):
                        logger.warning(
                            "Backstop order status missing from batch response "
                            "for %s (idx=%d, statuses=%d), position may be unprotected",
                            extra.get("coin", "?"), idx, len(statuses),
                        )
                        continue
                    s = statuses[idx]
                    if not self._is_order_success(s):
                        logger.warning(
                            "Backstop order update failed for %s: %s, position "
                            "may be unprotected (section 6.8)",
                            extra.get("coin", "?"), s,
                        )
        elif status == "err":
            logger.error("Place batch error: %s", result.get("response", ""))
            if extra_requests:
                logger.warning(
                    "Backstop order update failed for batch errored, "
                    "position may be unprotected: %s",
                    result.get("response", ""),
                )

        return retries

    @staticmethod
    def _is_order_success(status_entry: Any) -> bool:
        """Check if a per-order status indicates success (resting or filled)."""
        if isinstance(status_entry, dict):
            return "resting" in status_entry or "filled" in status_entry
        s = str(status_entry).lower()
        return "resting" in s or "filled" in s

    def _track_alo_rejection(self, symbol: str) -> None:
        """Track ALO rejection count per minute window for alerting."""
        now_ms = int(time.time() * 1000)
        # Reset window every 60 seconds
        if now_ms - self._alo_rejection_window_start_ms > 60_000:
            self._alo_rejection_counts.clear()
            self._alo_rejection_window_start_ms = now_ms

        count = self._alo_rejection_counts.get(symbol, 0) + 1
        self._alo_rejection_counts[symbol] = count
        if count > 5:
            logger.warning(
                "High ALO rejection rate for %s: %d in current window",
                symbol,
                count,
            )

    async def cancel_orders(
        self, symbol: str, orders: list[OpenOrder]
    ) -> None:
        """Batch cancel a specific list of orders by oid."""
        if not orders:
            return
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)
        cancel_requests = [
            {"coin": coin, "oid": int(o.order_id)} for o in orders
        ]
        try:
            result = await loop.run_in_executor(
                None, self._client.bulk_cancel, cancel_requests
            )
            logger.info(
                "Cancelled %d targeted orders for %s", len(cancel_requests), symbol
            )
            self._parse_cancel_result(result, orders)
        except Exception:
            logger.exception("cancel_orders failed for %s", symbol)

    async def cancel_all_orders(self, symbol: str) -> None:
        """Cancel all resting orders for an asset (batch cancel)."""
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)

        all_open = await loop.run_in_executor(
            None, self._info.open_orders, self._wallet_address
        )

        symbol_orders = [o for o in all_open if o.get("coin") == coin]
        if not symbol_orders:
            logger.info("No open orders to cancel for %s", symbol)
            return

        cancel_requests = [
            {"coin": coin, "oid": int(o["oid"])} for o in symbol_orders
        ]

        try:
            result = await loop.run_in_executor(
                None, self._client.bulk_cancel, cancel_requests
            )
            logger.info(
                "Cancelled %d orders for %s", len(cancel_requests), symbol
            )
            self._parse_cancel_result(
                result,
                [
                    OpenOrder(
                        order_id=int(o["oid"]),
                        client_order_id="",
                        symbol=symbol,
                        price=float(o.get("limitPx", 0)),
                        size=float(o.get("sz", 0)),
                        remaining=float(o.get("sz", 0)),
                        side=OrderSide.BUY if o.get("side") == "B" else OrderSide.SELL,
                    )
                    for o in symbol_orders
                ],
            )
        except Exception:
            logger.exception("cancel_all_orders failed for %s", symbol)

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

        For buys: lower the price (farther below mid).
        For sells: raise the price (farther above mid).
        Max retries: post_only_max_retries per level per cycle.
        Returns adjusted order, or None if max retries exceeded.
        """
        asset_cfg = self._get_asset_config(order.symbol)
        if attempt >= asset_cfg.post_only_max_retries:
            logger.warning(
                "ALO rejection exhausted retries for %s %s @ %.2f (attempt %d)",
                order.symbol,
                order.side.value,
                order.price,
                attempt,
            )
            return None

        tick = asset_cfg.tick_size
        if order.side == OrderSide.BUY:
            new_price = order.price - tick
        else:
            new_price = order.price + tick

        logger.info(
            "ALO nudge %s %s: %.2f -> %.2f (attempt %d)",
            order.symbol,
            order.side.value,
            order.price,
            new_price,
            attempt + 1,
        )

        return DesiredOrder(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            price=new_price,
            size=order.size,
            side=order.side,
            time_in_force=order.time_in_force,
            reduce_only=order.reduce_only,
        )

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
        If at hard cap: reduce-only order to unwind.
        Returns None if partial fill (don't flip on partials).
        """
        if fill.is_partial:
            return None

        if fill.side == OrderSide.BUY:
            flip_side = OrderSide.SELL
            flip_price = fill.price * (1 + step_bps / 10_000)
        else:
            flip_side = OrderSide.BUY
            flip_price = fill.price * (1 - step_bps / 10_000)

        # Generate deterministic flip order ID
        flip_id_raw = f"flip|{fill.fill_id}|{fill.symbol}|{flip_side.value}"
        digest = hashlib.sha256(flip_id_raw.encode()).hexdigest()
        flip_cloid = f"0x{digest[:32]}"

        return DesiredOrder(
            client_order_id=flip_cloid,
            symbol=fill.symbol,
            price=flip_price,
            size=fill.size,
            side=flip_side,
            time_in_force=TimeInForce.ALO,
            reduce_only=inventory_zone_is_hard_cap,
        )

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
        config_hash: str = "",
    ) -> None:
        """Maintain server-side trigger stop-loss for dead-man's switch.

        - Direction: opposite to current position
        - Size: full current position size
        - Trigger: anchor +/- (breakout_atr_distance + backstop_buffer_atr) * ATR
        - Type: trigger market, reduce-only, tpsl="sl"

        Cancel and replace on every position change.
        Remove when position reaches zero.

        Args:
            config_hash: Grid config hash from GridEngine.compute_config_hash().
                         Used for deterministic backstop order ID per design
                         doc section 6.8.
        """
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)

        # Determine position direction
        pos_size = position.size if position else 0.0
        if abs(pos_size) < 1e-12:
            # No position, cancel backstops for both directions
            for d in ("long", "short"):
                cloid = self._generate_backstop_id(symbol, d, config_hash)
                await self._cancel_existing_backstop(symbol, expected_cloid=cloid)
            return

        direction = "long" if pos_size > 0 else "short"
        is_buy = pos_size < 0  # Short position -> buy to close

        # Compute trigger price
        distance = (breakout_atr_distance + backstop_buffer_atr) * atr
        if pos_size > 0:
            # Long position: stop-loss triggers below anchor
            trigger_price = anchor - distance
        else:
            # Short position: stop-loss triggers above anchor
            trigger_price = anchor + distance

        # Generate deterministic backstop order ID (design doc section 6.8)
        backstop_cloid = self._generate_backstop_id(symbol, direction, config_hash)

        from hyperliquid.utils.types import Cloid

        # Cancel existing backstop(s) first, then place new one. Cancel BOTH
        # direction cloids, not just the current one: if position flipped
        # sign since the last update (long->short or vice versa), the old
        # direction's backstop has a different cloid and would otherwise be
        # left orphaned on the exchange, violating "do not leave orphaned
        # triggers" (section 6.8 item 3).
        other_direction = "short" if direction == "long" else "long"
        other_cloid = self._generate_backstop_id(symbol, other_direction, config_hash)
        await self._cancel_existing_backstop(symbol, expected_cloid=backstop_cloid)
        await self._cancel_existing_backstop(symbol, expected_cloid=other_cloid)

        # Place new backstop trigger order
        order_req = {
            "coin": coin,
            "is_buy": is_buy,
            "sz": abs(pos_size),
            "limit_px": trigger_price,
            "order_type": {
                "trigger": {
                    "triggerPx": trigger_price,
                    "isMarket": True,
                    "tpsl": "sl",
                }
            },
            "reduce_only": True,
            "cloid": Cloid.from_str(backstop_cloid),
        }

        try:
            result = await loop.run_in_executor(
                None, self._client.bulk_orders, [order_req]
            )
            if self._check_bulk_result(result, "backstop", symbol):
                logger.info(
                    "Backstop placed for %s: direction=%s trigger=%.2f size=%.6f",
                    symbol,
                    direction,
                    trigger_price,
                    abs(pos_size),
                )
        except Exception:
            logger.exception("Failed to place backstop for %s", symbol)

    async def _cancel_existing_backstop(
        self, symbol: str, expected_cloid: str = ""
    ) -> None:
        """Cancel an existing backstop trigger order matching the expected cloid."""
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)

        try:
            all_open = await loop.run_in_executor(
                None,
                self._info.frontend_open_orders,
                self._wallet_address,
            )
            backstops = [
                o
                for o in all_open
                if o.get("coin") == coin
                and o.get("cloid", "") == expected_cloid
            ]
            if backstops:
                cancel_requests = [
                    {"coin": coin, "oid": int(o["oid"])} for o in backstops
                ]
                await loop.run_in_executor(
                    None, self._client.bulk_cancel, cancel_requests
                )
                logger.info(
                    "Cancelled %d backstop(s) for %s",
                    len(backstops),
                    symbol,
                )
        except Exception:
            logger.exception(
                "Failed to cancel existing backstop for %s", symbol
            )

    # ------------------------------------------------------------------
    # Emergency flatten protocol (section 6.7)
    # ------------------------------------------------------------------

    async def execute_flatten(
        self,
        symbol: str,
        position: Position,
        config: AssetConfig,
        get_mid_price: Callable,
        get_book_depth: Callable,
        get_position: Callable,
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
        pos_size = abs(position.size)
        if pos_size < 1e-12:
            return True

        # Closing direction: sell to close longs, buy to close shorts
        close_side = OrderSide.SELL if position.size > 0 else OrderSide.BUY
        is_buy = close_side == OrderSide.BUY
        max_slippage_bps = config.max_flatten_slippage_bps
        time_budget = config.flatten_time_budget_seconds
        retry_pause_s = config.flatten_retry_pause_ms / 1000.0
        tranche_pct = config.flatten_tranche_pct

        # Step 1: Pre-flatten depth assessment
        try:
            available_depth = await get_book_depth(symbol, max_slippage_bps, close_side)
        except Exception:
            logger.warning(
                "Depth check failed for %s, proceeding with full size", symbol
            )
            available_depth = None

        if available_depth is not None and available_depth >= pos_size:
            tranche_size = pos_size
        elif available_depth is not None:
            tranche_size = available_depth * tranche_pct
        else:
            tranche_size = pos_size

        # Ensure tranche_size is positive
        tranche_size = max(tranche_size, 1e-8)

        # Step 2 + Step 3: IOC with retry loop
        flatten_start = time.time()
        remaining = pos_size
        min_order_size = 1e-6  # Minimum meaningful order size

        while remaining > min_order_size and (time.time() - flatten_start) < time_budget:
            try:
                mid_price = await get_mid_price(symbol)
            except Exception:
                logger.warning("Failed to get mid price during flatten, retrying")
                await asyncio.sleep(retry_pause_s)
                continue

            ioc_limit_price = self._compute_flatten_limit_price(
                mid_price, is_buy, max_slippage_bps
            )
            send_size = min(remaining, tranche_size)

            await self._send_flatten_ioc(symbol, close_side, send_size, ioc_limit_price)

            # Wait briefly for fill confirmation
            await asyncio.sleep(min(2.0, retry_pause_s))

            # Re-query position from exchange (always trust exchange)
            try:
                current_pos = await get_position(symbol)
                remaining = abs(current_pos.size) if current_pos else 0.0
            except Exception:
                logger.warning("Failed to re-query position during flatten, retrying")
                await asyncio.sleep(retry_pause_s)
                continue

            if remaining > min_order_size:
                # Refresh tranche size from book depth for next iteration
                try:
                    refreshed_depth = await get_book_depth(symbol, max_slippage_bps, close_side)
                    if refreshed_depth is not None and refreshed_depth > 0:
                        tranche_size = max(
                            refreshed_depth * tranche_pct if refreshed_depth < remaining else remaining,
                            1e-8,
                        )
                except Exception:
                    pass  # keep previous tranche_size

                logger.warning(
                    "Partial flatten %s: %.6f remaining, retrying",
                    symbol,
                    remaining,
                )
                await asyncio.sleep(retry_pause_s)

        # Step 4: Slippage escalation
        if remaining > min_order_size:
            logger.warning(
                "Flatten escalation for %s: doubling slippage", symbol
            )
            try:
                mid_price = await get_mid_price(symbol)
                escalated_limit = self._compute_flatten_limit_price(
                    mid_price, is_buy, max_slippage_bps * 2
                )
                await self._send_flatten_ioc(
                    symbol, close_side, remaining, escalated_limit
                )
                await asyncio.sleep(2.0)

                current_pos = await get_position(symbol)
                remaining = abs(current_pos.size) if current_pos else 0.0
            except Exception:
                logger.exception("Flatten escalation failed for %s", symbol)

        # Step 5: Dead state check
        if remaining > min_order_size:
            logger.critical(
                "FLATTEN FAILED for %s: residual=%.6f, manual intervention required",
                symbol,
                remaining,
            )
            return False

        logger.info("Flatten complete for %s", symbol)
        return True

    @staticmethod
    def _compute_flatten_limit_price(
        mid_price: float,
        is_buy: bool,
        slippage_bps: float,
    ) -> float:
        """Compute IOC limit price with bounded slippage."""
        if is_buy:
            return mid_price * (1 + slippage_bps / 10_000)
        else:
            return mid_price * (1 - slippage_bps / 10_000)

    async def _send_flatten_ioc(
        self,
        symbol: str,
        side: OrderSide,
        size: float,
        limit_price: float,
    ) -> None:
        """Send a single IOC reduce-only order for flattening.

        Submitted as a single order (not batch), flatten is urgent.
        """
        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)

        order_req = {
            "coin": coin,
            "is_buy": side == OrderSide.BUY,
            "sz": size,
            "limit_px": limit_price,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": True,
        }

        try:
            result = await loop.run_in_executor(
                None, self._client.bulk_orders, [order_req]
            )
            if self._check_bulk_result(result, "flatten IOC", symbol):
                logger.info(
                    "Flatten IOC sent: %s %s %.6f @ %.2f",
                    symbol,
                    side.value,
                    size,
                    limit_price,
                )
        except Exception:
            logger.exception(
                "Flatten IOC failed: %s %s %.6f @ %.2f",
                symbol,
                side.value,
                size,
                limit_price,
            )
