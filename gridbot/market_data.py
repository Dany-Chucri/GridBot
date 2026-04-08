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
import math
import time
from collections import deque
from functools import partial
from typing import Any

from hyperliquid.info import Info

from gridbot.config import BotConfig
from gridbot.types import Fill, OpenOrder, OrderSide, Position, VolMetrics

logger = logging.getLogger(__name__)

# Hyperliquid fee rates (standard tier)
_MAKER_FEE_RATE = 0.0002  # 0.02% = 0.2 bps
_TAKER_FEE_RATE = 0.0005  # 0.05% = 0.5 bps

# Conservative defaults when insufficient data
_DEFAULT_REALIZED_VOL = 1.0  # 100% annualized
_DEFAULT_SPREAD_BPS = 50.0
_DEFAULT_ROLLING_RETURN = 0.0

# Minimum data requirements
_MIN_TRADES_FOR_VOL = 30
_MIN_CANDLES_FOR_ATR = 14

# Sampling interval for vol annualization (seconds)
_VOL_SAMPLE_INTERVAL_S = 5

# Seconds per year
_SECONDS_PER_YEAR = 365 * 24 * 3600

# Reconnection parameters
_RECONNECT_BASE_DELAY_S = 1.0
_RECONNECT_MAX_DELAY_S = 60.0
_WS_HEALTH_CHECK_INTERVAL_S = 5.0

# Staleness threshold before falling back to allMids (ms)
_L2_STALENESS_MS = 10_000


class MarketData:
    """Maintains the real-time market view for all assets."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()

        # Per-asset latest prices
        self._mid_prices: dict[str, float] = {}
        self._mark_prices: dict[str, float] = {}
        self._funding_rates: dict[str, float] = {}
        self._best_bid: dict[str, float] = {}
        self._best_ask: dict[str, float] = {}

        # Per-asset vol computation buffers
        self._return_buffers: dict[str, deque[tuple[int, float]]] = {}
        self._minute_candles: dict[str, deque[dict]] = {}
        self._current_candle: dict[str, dict | None] = {}

        # Connection state
        self._ws_connected = False
        self._last_ws_message_ms: int = 0
        self._last_l2_update_ms: dict[str, int] = {}

        # SDK client
        self._info: Info | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Subscription tracking for cleanup
        self._subscriptions: list[tuple[dict, int]] = []

        # Async message queue (WS thread → async processor)
        self._ws_queue: asyncio.Queue[tuple[str, Any]] | None = None
        self._processor_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None

        # Public fills queue (consumed by Supervisor)
        self.fills: asyncio.Queue[Fill] | None = None

        # Order tracking for fill detection from orderUpdates
        self._tracked_orders: dict[int, dict] = {}

        # Coin ↔ symbol mapping
        self._coin_to_symbol: dict[str, str] = {}
        self._symbol_to_coin: dict[str, str] = {}
        for ac in config.assets:
            coin = ac.symbol.replace("-PERP", "").upper()
            self._coin_to_symbol[coin] = ac.symbol
            self._symbol_to_coin[ac.symbol] = coin

        # Initialize per-asset buffers
        for ac in config.assets:
            sym = ac.symbol
            self._return_buffers[sym] = deque(maxlen=10_000)
            self._minute_candles[sym] = deque(maxlen=10_080)  # ~7 days
            self._current_candle[sym] = None
            self._last_l2_update_ms[sym] = 0

        # Reconnection state
        self._reconnect_attempts = 0
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Coin/symbol helpers
    # ------------------------------------------------------------------

    def _to_coin(self, symbol: str) -> str:
        """'BTC-PERP' → 'BTC'."""
        return self._symbol_to_coin.get(symbol, symbol.replace("-PERP", "").upper())

    def _to_symbol(self, coin: str) -> str:
        """'BTC' → 'BTC-PERP'. Returns empty string if unknown."""
        return self._coin_to_symbol.get(coin.upper(), "")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish WS connections and subscribe to channels."""
        self._loop = asyncio.get_running_loop()
        self._ws_queue = asyncio.Queue()
        self.fills = asyncio.Queue()
        self._shutting_down = False
        self._reconnect_attempts = 0

        await self._establish_connection()

        # Start background tasks
        self._processor_task = asyncio.create_task(self._process_messages())
        self._monitor_task = asyncio.create_task(self._monitor_ws_health())

    async def _establish_connection(self) -> None:
        """Create SDK Info client and subscribe to all channels."""
        base_url = self._config.base_url

        # Info.__init__ does sync REST calls (meta fetch), run in executor
        self._info = await self._loop.run_in_executor(
            None, partial(Info, base_url=base_url, skip_ws=False)
        )

        for ac in self._config.assets:
            coin = self._to_coin(ac.symbol)

            # l2Book — primary price source (bid/ask/mid)
            sub_id = self._info.subscribe(
                {"type": "l2Book", "coin": coin}, self._on_l2_book
            )
            self._subscriptions.append(({"type": "l2Book", "coin": coin}, sub_id))

            # trades — for vol computation
            sub_id = self._info.subscribe(
                {"type": "trades", "coin": coin}, self._on_trades
            )
            self._subscriptions.append(({"type": "trades", "coin": coin}, sub_id))

            # activeAssetCtx — mark price, funding rate
            sub_id = self._info.subscribe(
                {"type": "activeAssetCtx", "coin": coin}, self._on_asset_ctx
            )
            self._subscriptions.append(({"type": "activeAssetCtx", "coin": coin}, sub_id))

        # allMids — fallback mid prices
        sub_id = self._info.subscribe({"type": "allMids"}, self._on_all_mids)
        self._subscriptions.append(({"type": "allMids"}, sub_id))

        # orderUpdates — fill detection (requires wallet address)
        if self._config.wallet_address:
            sub_id = self._info.subscribe(
                {"type": "orderUpdates", "user": self._config.wallet_address},
                self._on_order_updates,
            )
            self._subscriptions.append((
                {"type": "orderUpdates", "user": self._config.wallet_address},
                sub_id,
            ))
        else:
            logger.warning("No wallet_address configured — orderUpdates subscription skipped")

        self._ws_connected = True
        self._last_ws_message_ms = int(time.time() * 1000)
        logger.info("MarketData WS connected and subscribed")

    async def disconnect(self) -> None:
        """Cleanly close WS connections."""
        self._shutting_down = True
        self._ws_connected = False

        for task in (self._processor_task, self._monitor_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._info is not None:
            try:
                await self._loop.run_in_executor(None, self._info.disconnect_websocket)
            except Exception:
                logger.debug("Error disconnecting WebSocket", exc_info=True)
            self._info = None

        self._subscriptions.clear()
        self._tracked_orders.clear()
        logger.info("MarketData disconnected")

    # ------------------------------------------------------------------
    # WS callbacks (sync, called from WS thread → enqueue for async)
    # ------------------------------------------------------------------

    def _enqueue(self, msg_type: str, msg: Any) -> None:
        if self._loop and self._ws_queue:
            self._loop.call_soon_threadsafe(self._ws_queue.put_nowait, (msg_type, msg))

    def _on_l2_book(self, msg: dict) -> None:
        self._enqueue("l2Book", msg)

    def _on_trades(self, msg: dict) -> None:
        self._enqueue("trades", msg)

    def _on_all_mids(self, msg: dict) -> None:
        self._enqueue("allMids", msg)

    def _on_order_updates(self, msg: dict) -> None:
        self._enqueue("orderUpdates", msg)

    def _on_asset_ctx(self, msg: dict) -> None:
        self._enqueue("activeAssetCtx", msg)

    # ------------------------------------------------------------------
    # Async message processor
    # ------------------------------------------------------------------

    async def _process_messages(self) -> None:
        """Drain the WS queue and dispatch to typed handlers."""
        while not self._shutting_down:
            try:
                msg_type, msg = await asyncio.wait_for(self._ws_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                self._last_ws_message_ms = int(time.time() * 1000)

                if msg_type == "l2Book":
                    await self._dispatch_l2_book(msg)
                elif msg_type == "trades":
                    await self._dispatch_trades(msg)
                elif msg_type == "allMids":
                    await self._dispatch_all_mids(msg)
                elif msg_type == "orderUpdates":
                    await self._dispatch_order_updates(msg)
                elif msg_type == "activeAssetCtx":
                    await self._dispatch_asset_ctx(msg)
            except Exception:
                logger.error("Error processing %s message", msg_type, exc_info=True)

    async def _dispatch_l2_book(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        symbol = self._to_symbol(coin)
        if not symbol:
            return

        levels = data.get("levels", [[], []])
        bids, asks = levels[0], levels[1]
        if bids and asks:
            bid = float(bids[0]["px"])
            ask = float(asks[0]["px"])
            await self._handle_price_update(symbol, bid, ask)
            self._last_l2_update_ms[symbol] = int(time.time() * 1000)

    async def _dispatch_trades(self, msg: dict) -> None:
        for trade in msg.get("data", []):
            coin = trade.get("coin", "")
            symbol = self._to_symbol(coin)
            if not symbol:
                continue
            await self._handle_trade(
                symbol, float(trade["px"]), float(trade["sz"]), trade["time"]
            )

    async def _dispatch_all_mids(self, msg: dict) -> None:
        mids = msg.get("data", {}).get("mids", {})
        now_ms = int(time.time() * 1000)
        async with self._lock:
            for coin, price_str in mids.items():
                symbol = self._to_symbol(coin)
                if not symbol:
                    continue
                last_l2 = self._last_l2_update_ms.get(symbol, 0)
                if now_ms - last_l2 > _L2_STALENESS_MS:
                    self._mid_prices[symbol] = float(price_str)

    async def _dispatch_order_updates(self, msg: dict) -> None:
        for update in msg.get("data", []):
            fill = await self._handle_order_update(update)
            if fill is not None and self.fills is not None:
                await self.fills.put(fill)

    async def _dispatch_asset_ctx(self, msg: dict) -> None:
        data = msg.get("data", {})
        coin = data.get("coin", "")
        symbol = self._to_symbol(coin)
        if not symbol:
            return
        ctx = data.get("ctx", {})
        mark_px = ctx.get("markPx")
        funding = ctx.get("funding")
        async with self._lock:
            if mark_px:
                self._mark_prices[symbol] = float(mark_px)
            if funding:
                self._funding_rates[symbol] = float(funding)

    # ------------------------------------------------------------------
    # WS health monitor with reconnection
    # ------------------------------------------------------------------

    async def _monitor_ws_health(self) -> None:
        """Periodic check for WS liveness; reconnect with backoff if stale."""
        while not self._shutting_down:
            try:
                await asyncio.sleep(_WS_HEALTH_CHECK_INTERVAL_S)
                if self._shutting_down:
                    break

                now_ms = int(time.time() * 1000)
                stale_ms = self._config.operational.max_time_desynced_seconds * 1000

                if self._last_ws_message_ms > 0 and now_ms - self._last_ws_message_ms > stale_ms:
                    logger.warning(
                        "WS stale for %.1fs, reconnecting…",
                        (now_ms - self._last_ws_message_ms) / 1000,
                    )
                    await self._reconnect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("WS monitor error", exc_info=True)

    async def _reconnect(self) -> None:
        """Tear down and re-establish WS with exponential backoff."""
        self._ws_connected = False

        if self._info:
            try:
                await self._loop.run_in_executor(None, self._info.disconnect_websocket)
            except Exception:
                pass
            self._info = None
        self._subscriptions.clear()

        delay = min(
            _RECONNECT_BASE_DELAY_S * (2 ** self._reconnect_attempts),
            _RECONNECT_MAX_DELAY_S,
        )
        self._reconnect_attempts += 1
        logger.info("Reconnecting in %.1fs (attempt %d)", delay, self._reconnect_attempts)
        await asyncio.sleep(delay)

        try:
            await self._establish_connection()
            self._reconnect_attempts = 0
        except Exception:
            logger.error("Reconnection failed", exc_info=True)

    # ------------------------------------------------------------------
    # WS message handlers
    # ------------------------------------------------------------------

    async def _handle_price_update(self, symbol: str, bid: float, ask: float) -> None:
        """Process a new best bid/ask update."""
        async with self._lock:
            self._best_bid[symbol] = bid
            self._best_ask[symbol] = ask
            self._mid_prices[symbol] = (bid + ask) / 2

    async def _handle_trade(
        self, symbol: str, price: float, size: float, timestamp_ms: int
    ) -> None:
        """Process a trade message for vol/return calculation."""
        async with self._lock:
            # Append to return buffer
            self._return_buffers[symbol].append((timestamp_ms, price))

            # Aggregate into 1-minute candles
            candle_ts = (timestamp_ms // 60_000) * 60_000
            current = self._current_candle.get(symbol)

            if current is None or current["timestamp_ms"] != candle_ts:
                if current is not None:
                    self._minute_candles[symbol].append(current)
                self._current_candle[symbol] = {
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": size,
                    "timestamp_ms": candle_ts,
                }
            else:
                current["high"] = max(current["high"], price)
                current["low"] = min(current["low"], price)
                current["close"] = price
                current["volume"] += size

    async def _handle_order_update(self, raw: dict) -> Fill | None:
        """Process a WS orderUpdate — the PRIMARY fill detection path.

        Returns a Fill on full OR partial fill, None otherwise.
        Partial fills carry ``is_partial=True`` so the Supervisor records
        PnL but does not trigger a grid flip (design doc: flip only on
        full fill).
        """
        async with self._lock:
            order = raw.get("order", {})
            status = raw.get("status", "")
            status_ts = raw.get("statusTimestamp", int(time.time() * 1000))

            oid = order.get("oid")
            if oid is None:
                return None

            coin = order.get("coin", "")
            symbol = self._to_symbol(coin)
            if not symbol:
                return None

            limit_px = float(order.get("limitPx", "0"))
            remaining_sz = float(order.get("sz", "0"))
            orig_sz = float(order.get("origSz", "0"))
            side_raw = order.get("side", "")
            cloid = order.get("cloid") or ""
            side = OrderSide.BUY if side_raw == "B" else OrderSide.SELL

            # Determine maker/taker from time-in-force when available.
            # ALO (post-only) can only fill as maker; IOC is always taker.
            tif = order.get("tif", "")
            is_maker = tif.lower() != "ioc"

            if status == "filled":
                tracked = self._tracked_orders.pop(oid, None)
                fill_size = tracked["remaining_sz"] if tracked else orig_sz
                if tracked:
                    is_maker = tracked["is_maker"]
                fee_rate = _MAKER_FEE_RATE if is_maker else _TAKER_FEE_RATE

                return Fill(
                    fill_id=f"{oid}-{status_ts}",
                    order_id=oid,
                    client_order_id=cloid,
                    symbol=symbol,
                    price=limit_px,
                    size=fill_size,
                    side=side,
                    fee=fill_size * limit_px * fee_rate,
                    timestamp_ms=status_ts,
                    is_maker=is_maker,
                    is_partial=False,
                )

            if status == "open":
                tracked = self._tracked_orders.get(oid)
                if tracked is None:
                    self._tracked_orders[oid] = {
                        "remaining_sz": remaining_sz,
                        "orig_sz": orig_sz,
                        "coin": coin,
                        "side": side,
                        "limit_px": limit_px,
                        "cloid": cloid,
                        "is_maker": is_maker,
                    }
                    return None

                # Partial fill: remaining decreased since last update
                prev_remaining = tracked["remaining_sz"]
                fill_qty = prev_remaining - remaining_sz
                tracked["remaining_sz"] = remaining_sz

                if fill_qty > 0:
                    tracked_maker = tracked["is_maker"]
                    fee_rate = _MAKER_FEE_RATE if tracked_maker else _TAKER_FEE_RATE
                    return Fill(
                        fill_id=f"{oid}-partial-{status_ts}",
                        order_id=oid,
                        client_order_id=tracked["cloid"],
                        symbol=symbol,
                        price=limit_px,
                        size=fill_qty,
                        side=side,
                        fee=fill_qty * limit_px * fee_rate,
                        timestamp_ms=status_ts,
                        is_maker=tracked_maker,
                        is_partial=True,
                    )
                return None

            if status in ("canceled", "marginCanceled", "rejected"):
                self._tracked_orders.pop(oid, None)
                return None

            return None

    # ------------------------------------------------------------------
    # REST backup path (section 4.3)
    # ------------------------------------------------------------------

    async def fetch_open_orders(self, symbol: str) -> list[OpenOrder]:
        """REST fetch of current open orders — backup consistency check."""
        if not self._info or not self._config.wallet_address:
            return []

        loop = asyncio.get_running_loop()
        try:
            raw_orders = await loop.run_in_executor(
                None,
                partial(self._info.frontend_open_orders, self._config.wallet_address),
            )
        except Exception:
            logger.error("Failed to fetch open orders for %s", symbol, exc_info=True)
            return []

        coin = self._to_coin(symbol).upper()
        result: list[OpenOrder] = []
        for o in raw_orders:
            if o.get("coin", "").upper() != coin:
                continue
            side = OrderSide.BUY if o.get("side") == "B" else OrderSide.SELL
            result.append(OpenOrder(
                order_id=o["oid"],
                client_order_id=o.get("cloid", ""),
                symbol=symbol,
                price=float(o["limitPx"]),
                size=float(o.get("origSz", o["sz"])),
                remaining=float(o["sz"]),
                side=side,
                reduce_only=o.get("reduceOnly", False),
            ))
        return result

    async def fetch_position(self, symbol: str) -> Position | None:
        """REST fetch of current position — backup consistency check."""
        if not self._info or not self._config.wallet_address:
            return None

        loop = asyncio.get_running_loop()
        try:
            state = await loop.run_in_executor(
                None,
                partial(self._info.user_state, self._config.wallet_address),
            )
        except Exception:
            logger.error("Failed to fetch position for %s", symbol, exc_info=True)
            return None

        coin = self._to_coin(symbol).upper()
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin", "").upper() != coin:
                continue

            szi = float(pos.get("szi", "0"))
            if szi == 0:
                return None

            liq_px = pos.get("liquidationPx")
            return Position(
                symbol=symbol,
                size=szi,
                avg_entry_price=float(pos.get("entryPx") or "0"),
                unrealized_pnl=float(pos.get("unrealizedPnl", "0")),
                liquidation_price=float(liq_px) if liq_px else None,
            )
        return None

    async def fetch_exchange_pnl(self, symbol: str) -> float:
        """REST fetch of exchange-reported unrealized PnL (section 10.6)."""
        if not self._info or not self._config.wallet_address:
            return 0.0

        loop = asyncio.get_running_loop()
        try:
            state = await loop.run_in_executor(
                None,
                partial(self._info.user_state, self._config.wallet_address),
            )
        except Exception:
            logger.error("Failed to fetch exchange PnL for %s", symbol, exc_info=True)
            return 0.0

        coin = self._to_coin(symbol).upper()
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin", "").upper() != coin:
                continue
            return float(pos.get("unrealizedPnl", "0"))
        return 0.0

    # ------------------------------------------------------------------
    # Derived metrics (section 4.2)
    # ------------------------------------------------------------------

    def get_mid_price(self, symbol: str) -> float:
        """Latest mid price for the asset."""
        return self._mid_prices.get(symbol, 0.0)

    def get_mark_price(self, symbol: str) -> float:
        """Latest mark price for the asset."""
        return self._mark_prices.get(symbol, 0.0)

    def get_funding_rate(self, symbol: str) -> float:
        """Latest funding rate for the asset."""
        return self._funding_rates.get(symbol, 0.0)

    def compute_vol_metrics(self, symbol: str) -> VolMetrics:
        """Compute current volatility metrics from buffered data."""
        mid = self._mid_prices.get(symbol, 0.0)
        bid = self._best_bid.get(symbol, 0.0)
        ask = self._best_ask.get(symbol, 0.0)

        # Spread
        if mid > 0 and bid > 0 and ask > 0:
            spread_bps = (ask - bid) / mid * 10_000
        else:
            spread_bps = _DEFAULT_SPREAD_BPS

        # Realized vol
        buf = self._return_buffers.get(symbol, deque())
        realized_vol = self._compute_realized_vol(buf, mid)

        # ATR proxy
        candles = self._minute_candles.get(symbol, deque())
        atr = self._compute_atr(candles, mid)

        # Rolling returns
        rolling_1m = self._compute_rolling_return(buf, 60_000)
        rolling_5m = self._compute_rolling_return(buf, 300_000)

        return VolMetrics(
            realized_vol=realized_vol,
            atr=atr,
            spread_bps=spread_bps,
            rolling_return_1m=rolling_1m,
            rolling_return_5m=rolling_5m,
        )

    # ------------------------------------------------------------------
    # Vol helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_realized_vol(buf: deque[tuple[int, float]], mid: float) -> float:
        """Standard deviation of log returns, resampled at fixed intervals, annualized."""
        if len(buf) < _MIN_TRADES_FOR_VOL:
            return _DEFAULT_REALIZED_VOL

        entries = list(buf)
        start_ms, end_ms = entries[0][0], entries[-1][0]
        span_ms = end_ms - start_ms
        interval_ms = _VOL_SAMPLE_INTERVAL_S * 1000

        if span_ms < interval_ms * 2:
            return _DEFAULT_REALIZED_VOL

        # Resample to fixed intervals by last-known price
        sampled: list[float] = []
        idx = 0
        t = start_ms
        while t <= end_ms:
            while idx + 1 < len(entries) and entries[idx + 1][0] <= t:
                idx += 1
            sampled.append(entries[idx][1])
            t += interval_ms

        if len(sampled) < 2:
            return _DEFAULT_REALIZED_VOL

        # Log returns
        log_rets: list[float] = []
        for i in range(1, len(sampled)):
            if sampled[i - 1] > 0 and sampled[i] > 0:
                log_rets.append(math.log(sampled[i] / sampled[i - 1]))

        if len(log_rets) < 2:
            return _DEFAULT_REALIZED_VOL

        mean_r = sum(log_rets) / len(log_rets)
        variance = sum((r - mean_r) ** 2 for r in log_rets) / (len(log_rets) - 1)
        std_dev = math.sqrt(variance)

        periods_per_year = _SECONDS_PER_YEAR / _VOL_SAMPLE_INTERVAL_S
        return std_dev * math.sqrt(periods_per_year)

    @staticmethod
    def _compute_atr(candles: deque[dict], mid: float) -> float:
        """ATR proxy: average true range from the last N minute candles."""
        if len(candles) < _MIN_CANDLES_FOR_ATR:
            return mid * 0.02 if mid > 0 else 0.0

        recent = list(candles)[-_MIN_CANDLES_FOR_ATR:]
        true_ranges: list[float] = []
        for i, c in enumerate(recent):
            high, low = c["high"], c["low"]
            if i > 0:
                prev_close = recent[i - 1]["close"]
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            else:
                tr = high - low
            true_ranges.append(tr)

        return sum(true_ranges) / len(true_ranges)

    @staticmethod
    def _compute_rolling_return(buf: deque[tuple[int, float]], window_ms: int) -> float:
        """Price return over the specified lookback window."""
        if len(buf) < 2:
            return _DEFAULT_ROLLING_RETURN

        entries = list(buf)
        current_price = entries[-1][1]
        target_ms = entries[-1][0] - window_ms

        # Find the closest price at or before target_ms
        past_price = entries[0][1]
        for ts, px in entries:
            if ts <= target_ms:
                past_price = px
            else:
                break

        if past_price <= 0:
            return _DEFAULT_ROLLING_RETURN

        return (current_price - past_price) / past_price

    # ------------------------------------------------------------------
    # L2 book depth (for flatten slippage estimation, section 5.7)
    # ------------------------------------------------------------------

    async def fetch_book_depth(
        self, symbol: str, depth_bps: float = 50.0, side: OrderSide | None = None
    ) -> float:
        """Get available depth within depth_bps of mid price.

        Args:
            side: The side you intend to *trade*.  ``SELL`` sums bid-side
                  depth (you sell into resting bids).  ``BUY`` sums
                  ask-side depth (you buy from resting asks).  ``None``
                  sums both sides.

        Used by the emergency flatten protocol (section 6.7) for
        pre-flatten depth assessment.
        """
        if not self._info:
            return 0.0

        loop = asyncio.get_running_loop()
        coin = self._to_coin(symbol)
        try:
            snapshot = await loop.run_in_executor(
                None, partial(self._info.l2_snapshot, coin)
            )
        except Exception:
            logger.error("Failed to fetch book depth for %s", symbol, exc_info=True)
            return 0.0

        levels = snapshot.get("levels", [[], []])
        bids, asks = levels[0], levels[1]

        mid = self._mid_prices.get(symbol, 0.0)
        if mid <= 0:
            return 0.0

        depth_threshold = mid * depth_bps / 10_000
        total = 0.0

        if side is None or side is OrderSide.SELL:
            for level in bids:
                if mid - float(level["px"]) <= depth_threshold:
                    total += float(level["sz"])

        if side is None or side is OrderSide.BUY:
            for level in asks:
                if float(level["px"]) - mid <= depth_threshold:
                    total += float(level["sz"])

        return total
