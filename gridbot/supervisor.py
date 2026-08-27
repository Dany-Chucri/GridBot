"""Supervisor: orchestration and lifecycle.

Responsibilities (design doc section 3.2):
- Run the main event loop
- Coordinate module execution order each cycle (section 3.3)
- Handle periodic REST reconciliation
- Manage alerting and structured logging
- Implement graceful shutdown (section 4.5)
- Execute restart recovery sequence (section 4.4)

Data flow per cycle (section 3.3):
1. MarketData updates price, vol, spread (from WS)
2. MarketData processes any new fills (from WS orderUpdates)
3. RiskManager evaluates regime, circuit breakers, funding, drawdown
4. IF risk check fails -> cancel orders, flatten if needed, enter cooldown
5. IF risk check passes:
   a. GridEngine computes desired order set
   b. OrderManager computes diff against current open orders
   c. OrderManager submits batch operation
6. StateStore persists updated state
7. PnL Monitor cross-checks with exchange (on schedule)
8. Supervisor logs cycle metrics
"""

from __future__ import annotations

import asyncio
import logging
import time

from gridbot.config import AssetConfig, BotConfig
from gridbot.grid_engine import GridEngine
from gridbot.market_data import MarketData
from gridbot.order_manager import OrderManager
from gridbot.pnl_monitor import PnLMonitor
from gridbot.risk_manager import RiskAction, RiskDecision, RiskManager
from gridbot.state_store import StateStore
from gridbot.types import (
    AlertCallback,
    AssetState,
    BotState,
    Fill,
    InventoryZone,
    PendingFlip,
    Position,
    Regime,
    VolMetrics,
)

logger = logging.getLogger(__name__)


_INITIAL_WS_TIMEOUT_S = 30.0
_WS_STALE_MS = 10_000
_BREAKOUT_DETAIL_TYPES = frozenset({"distance", "return_5m", "vol_spike"})
_RISK_ACTION_HEARTBEAT_MS = 10 * 60 * 1000


class Supervisor:
    """Main orchestrator, coordinates all modules through the event loop."""

    def __init__(
        self,
        config: BotConfig,
        *,
        state_store: StateStore | None = None,
        market_data: MarketData | None = None,
        order_manager: OrderManager | None = None,
        risk_manager: RiskManager | None = None,
        pnl_monitor: PnLMonitor | None = None,
        alert_callback: AlertCallback | None = None,
    ) -> None:
        self._config = config
        self._shutdown_requested = False

        # Module instances (allow injection for testing)
        self._market_data = market_data or MarketData(config)
        self._state_store = state_store or StateStore()
        self._order_manager = order_manager or OrderManager(config)
        self._risk_manager = risk_manager or RiskManager(config)
        self._pnl_monitor = pnl_monitor or PnLMonitor(config)

        # Per-asset grid engines and state
        self._grid_engines: dict[str, GridEngine] = {}
        self._asset_states: dict[str, AssetState] = {}

        for asset_cfg in config.assets:
            self._grid_engines[asset_cfg.symbol] = GridEngine(
                asset_cfg, config.operational
            )
            self._asset_states[asset_cfg.symbol] = AssetState(
                symbol=asset_cfg.symbol
            )

        # Per-asset timers for independent cadences
        self._last_rest_reconcile_ms: dict[str, int] = {
            ac.symbol: 0 for ac in config.assets
        }
        self._last_crosscheck_ms: dict[str, int] = {
            ac.symbol: 0 for ac in config.assets
        }
        self._last_regime: dict[str, Regime] = {
            ac.symbol: Regime.UNKNOWN for ac in config.assets
        }

        # Risk-action log throttling: log on entry (action changes), on a
        # heartbeat cadence while unchanged, and once on exit (back to
        # CONTINUE), not on every cycle a persistent condition is evaluated.
        self._last_risk_action: dict[str, RiskAction | None] = {
            ac.symbol: None for ac in config.assets
        }
        self._risk_action_last_log_ms: dict[str, int] = {}

        # Pluggable alert transport
        self._alert_callback: AlertCallback | None = alert_callback

        # Fill pump task
        self._fill_task: asyncio.Task | None = None

        # Wall-clock timestamp of the current MAINTENANCE episode's entry
        # (None when not in maintenance). Logged as a duration on exit so a
        # creeping maintenance window shows up before it's long enough to
        # blow past _MAX_CONTINUOUS_GAP_MS and reset vol-history bootstrap.
        self._maintenance_entered_ms: int | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point: initialize, recover, and run the event loop."""
        try:
            await self._initialize()
            await self._recover_state()
            await self._preflight_checks()
            await self._main_loop()
        finally:
            await self._shutdown()

    def request_shutdown(self) -> None:
        """Signal graceful shutdown (called from signal handler)."""
        logger.info("Shutdown requested")
        self._shutdown_requested = True

    def set_alert_callback(self, cb: AlertCallback) -> None:
        """Install a pluggable alert transport (Telegram/Discord/etc)."""
        self._alert_callback = cb
        self._market_data.set_alert_callback(cb)

    # ------------------------------------------------------------------
    # Initialization & recovery
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        """Initialize all modules (section 4.4)."""
        logger.info("Initializing modules")
        await self._state_store.initialize()
        await self._order_manager.initialize()
        await self._market_data.connect()

        # Wait for first WS price data before proceeding (bounded)
        deadline = time.monotonic() + _INITIAL_WS_TIMEOUT_S
        while time.monotonic() < deadline:
            if all(
                self._market_data.get_mid_price(ac.symbol) > 0
                for ac in self._config.assets
            ):
                break
            await asyncio.sleep(0.25)
        else:
            missing = [
                ac.symbol for ac in self._config.assets
                if self._market_data.get_mid_price(ac.symbol) <= 0
            ]
            if missing:
                raise RuntimeError(
                    f"Timed out waiting for initial WS price for: {missing}"
                )

        # Start fill pump (section 7.12)
        self._fill_task = asyncio.create_task(self._fill_pump())

        logger.info("Initialization complete")

    async def _recover_state(self) -> None:
        """Restart recovery sequence (section 4.4)."""
        logger.info("Recovering state")
        for asset_cfg in self._config.assets:
            symbol = asset_cfg.symbol

            # 1. Load persisted state
            persisted = await self._state_store.load_bot_state(symbol)
            if persisted is not None:
                self._asset_states[symbol] = persisted

            state = self._asset_states[symbol]

            # Restore pending flips from store (source of truth)
            state.pending_flips = await self._state_store.load_pending_flips(symbol)

            # Restore grid_config (may be separate row)
            grid_cfg = await self._state_store.load_grid_config(symbol)
            if grid_cfg is not None:
                state.grid_config = grid_cfg

            # Restore vol history so a restart doesn't cost a fresh 48h
            # bootstrap on top of real, already-accumulated data. Gap-aware
            # sufficiency (_continuous_run) decides whether it's still
            # usable, a short gap (ordinary restart) is transparent, a
            # long one (real outage) correctly forces a fresh bootstrap.
            vol_samples = await self._state_store.load_vol_history(symbol)
            self._risk_manager.load_vol_history(
                symbol, vol_samples, int(time.time() * 1000)
            )

            # 2. Fetch exchange state
            exchange_orders = await self._market_data.fetch_open_orders(symbol)
            exchange_position = await self._market_data.fetch_position(symbol)

            # 3. Reconcile, adopt exchange as truth, cancel orphans
            persisted_cloids = {
                o.client_order_id for o in state.open_orders if o.client_order_id
            }
            orphans = [
                o for o in exchange_orders
                if o.client_order_id and o.client_order_id not in persisted_cloids
            ]
            if orphans:
                logger.warning(
                    "Found %d orphan orders on exchange for %s, cancelling",
                    len(orphans), symbol,
                )
                await self._order_manager.cancel_orders(symbol, orphans)
                exchange_orders = await self._market_data.fetch_open_orders(symbol)

            # Orders in local state but no longer on the exchange snapshot
            # must be checked against the fills endpoint (section 4.4 step
            # 4): they may have filled while the bot was down, not merely
            # been cancelled. Without this, a fill during downtime is
            # silently dropped, no flip is ever placed for that level and
            # local PnL under-counts it.
            exchange_cloids = {o.client_order_id for o in exchange_orders if o.client_order_id}
            vanished = [
                o for o in state.open_orders
                if o.client_order_id and o.client_order_id not in exchange_cloids
            ]

            state.open_orders = exchange_orders
            state.position = exchange_position

            if vanished:
                last_hb = await self._state_store.get_last_heartbeat(symbol)
                since_ms = last_hb if last_hb is not None else 0
                fills = await self._market_data.fetch_fills(symbol, since_ms)
                fills_by_oid = {f.order_id: f for f in fills}
                for order in vanished:
                    matched = fills_by_oid.get(order.order_id)
                    if matched is None:
                        continue
                    logger.warning(
                        "Order %d for %s filled while bot was down, routing missed fill",
                        order.order_id, symbol,
                    )
                    missed_fill = Fill(
                        fill_id=matched.fill_id,
                        order_id=matched.order_id,
                        client_order_id=order.client_order_id,
                        symbol=symbol,
                        price=matched.price,
                        size=matched.size,
                        side=matched.side,
                        fee=matched.fee,
                        timestamp_ms=matched.timestamp_ms,
                        is_maker=matched.is_maker,
                        is_partial=False,
                    )
                    await self._route_fill(missed_fill)

            # 4. Resume FLATTENING if persisted and position remains
            if state.bot_state == BotState.FLATTENING:
                if exchange_position is not None and abs(exchange_position.size) > 0:
                    logger.warning(
                        "Resuming FLATTENING for %s (pos=%.6f)",
                        symbol, exchange_position.size,
                    )
                    await self._run_flatten(symbol, asset_cfg)
                else:
                    state.bot_state = BotState.STARTING

            # Start from a clean run state if nothing else dictated otherwise
            if state.bot_state in (BotState.DEAD, BotState.SHUTTING_DOWN):
                # Preserve DEAD; treat SHUTTING_DOWN as fresh start
                if state.bot_state == BotState.SHUTTING_DOWN:
                    state.bot_state = BotState.STARTING
            else:
                state.bot_state = BotState.STARTING

        logger.info("State recovery complete")

    async def _preflight_checks(self) -> None:
        """Run pre-flight validation (section 6.1).

        Hard gate, no operator override allowed.
        """
        logger.info("Running pre-flight checks")
        equity = await self._market_data.fetch_account_equity()
        if equity is None or equity <= 0:
            raise RuntimeError(f"Pre-flight: account_equity={equity} is non-positive")

        all_violations: list[tuple[str, list[str]]] = []
        for asset_cfg in self._config.assets:
            mid_price = self._market_data.get_mid_price(asset_cfg.symbol)
            violations = self._risk_manager.preflight_check(asset_cfg, equity, mid_price)
            if violations:
                all_violations.append((asset_cfg.symbol, violations))

        if all_violations:
            for sym, vs in all_violations:
                for v in vs:
                    logger.error("Pre-flight violation [%s]: %s", sym, v)
            raise RuntimeError("Pre-flight checks failed, refusing to start")

        # All clear, transition assets to RUNNING
        for ac in self._config.assets:
            st = self._asset_states[ac.symbol]
            if st.bot_state not in (BotState.DEAD,):
                st.bot_state = BotState.RUNNING
        logger.info("Pre-flight checks passed (equity=%.2f)", equity)

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Core event loop, runs until shutdown or all assets DEAD."""
        logger.info("Entering main loop")
        while not self._shutdown_requested:
            if all(
                self._asset_states[ac.symbol].bot_state == BotState.DEAD
                for ac in self._config.assets
            ):
                logger.error("All assets DEAD, exiting main loop")
                break

            for asset_cfg in self._config.assets:
                if self._shutdown_requested:
                    break

                symbol = asset_cfg.symbol
                state = self._asset_states[symbol]

                if state.bot_state == BotState.DEAD:
                    continue

                try:
                    await self._run_cycle(symbol, asset_cfg)
                except Exception as exc:
                    if self._looks_like_maintenance(exc):
                        logger.warning(
                            "Cycle error for %s looks like maintenance: %s",
                            symbol, exc,
                        )
                        await self._handle_maintenance(symbol)
                    else:
                        logger.exception("Cycle failed for %s", symbol)
                        self._risk_manager.record_error()

            await asyncio.sleep(self._config.operational.cycle_interval_seconds)

    async def _run_cycle(self, symbol: str, asset_config: AssetConfig) -> None:
        """Execute one reconciliation cycle for a single asset (section 3.3)."""
        state = self._asset_states[symbol]
        now_ms = int(time.time() * 1000)

        # Respect COOLDOWN timer (keep heartbeat fresh so liveness check
        # doesn't page operators during long cooldowns). Still sample vol
        # and equity while waiting, a planned cooldown (default 30 min) is
        # not a data gap, only MAINTENANCE (a real exchange-side outage)
        # should be allowed to blow _MAX_CONTINUOUS_GAP_MS and reset
        # bootstrap, or leave a hole in the drawdown window. But COOLDOWN
        # skips the desync/maintenance checks below entirely, so a real
        # outage that happens to fall inside a cooldown window would
        # otherwise go undetected: skip sampling while the WS is stale so
        # a genuine outage still shows up as a gap to the continuity check
        # instead of being backfilled with repeated stale-price readings.
        if state.bot_state == BotState.COOLDOWN:
            await self._state_store.update_heartbeat(symbol, now_ms)
            if state.cooldown_until_ms is not None and now_ms < state.cooldown_until_ms:
                if not self._ws_is_stale(now_ms):
                    await self._record_equity_sample(state, now_ms)
                    await self._record_vol_sample(symbol, now_ms)
                return
            logger.info("Cooldown expired for %s, resuming", symbol)
            state.bot_state = BotState.RUNNING
            state.cooldown_until_ms = None

        if state.bot_state == BotState.MAINTENANCE:
            # If WS is healthy again, reconcile via REST and resume.
            if self._market_data.is_ws_connected() and not self._ws_is_stale(now_ms):
                logger.info("MAINTENANCE exit for %s, reconciling via REST", symbol)
                await self._rest_reconciliation(symbol)
                state.bot_state = BotState.RUNNING
                await self._state_store.update_heartbeat(symbol, now_ms)
                await self._log_maintenance_duration_if_done(now_ms)
            return

        # WS staleness feeds the desync kill switch in RiskManager.
        self._update_desync(now_ms)

        # 1. Exchange-reported equity (source of truth), see
        # _record_equity_sample for the None-read guard.
        await self._record_equity_sample(state, now_ms)

        # 2. Market data snapshot (also feeds vol history, see
        # _record_vol_sample, for percentile calcs and RiskManager._continuous_run)
        vol_metrics = await self._record_vol_sample(symbol, now_ms)
        state.vol_metrics = vol_metrics
        state.mid_price = self._market_data.get_mid_price(symbol)
        state.mark_price = self._market_data.get_mark_price(symbol)
        state.funding_rate = self._market_data.get_funding_rate(symbol)
        state.moving_avg = self._market_data.get_moving_average(symbol)

        # 3. Regime detection + risk evaluation
        state.regime = self._risk_manager.detect_regime(
            symbol,
            state.mid_price,
            vol_metrics,
            state.moving_avg,
            state.last_breakout_ms,
            now_ms,
            asset_config,
        )
        if state.regime != self._last_regime[symbol]:
            logger.info(
                "regime transition symbol=%s %s -> %s",
                symbol,
                self._last_regime[symbol].name,
                state.regime.name,
            )
            await self._send_alert(
                "INFO",
                f"{symbol} regime transition: "
                f"{self._last_regime[symbol].name} -> {state.regime.name}",
            )
            self._last_regime[symbol] = state.regime
        decision = self._risk_manager.evaluate(state)

        # RiskManager.evaluate() has no knowledge of regime, its breakout/
        # vol checks are independent triggers that happen to often coincide
        # with TREND/HIGH_VOL regime reads, but not always (e.g. a slow
        # grind away from the moving average trips TREND without crossing
        # the breakout-distance or vol-spike thresholds). Design section 8.1
        # requires TREND to cancel orders, flatten any inventory, and enter
        # cooldown, not just stop quoting. HIGH_VOL is already covered:
        # its regime threshold matches _check_volatility's vol_pause_percentile,
        # which independently returns PAUSE_GRID (existing orders remain
        # resting, per section 6.4, GridEngine's empty desired-set behavior
        # for non-RANGE regimes never even runs in that case).
        if decision.action == RiskAction.CONTINUE and state.regime == Regime.TREND:
            decision = RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="regime TREND: price diverged from moving average / recent breakout cooldown active",
                details={"type": "regime_trend"},
            )

        # UNKNOWN (insufficient continuous vol history, cold start, or a
        # restart after a gap wide enough that _continuous_run resets) gets
        # the same treatment as HIGH_VOL: pause new placement, leave
        # existing orders resting. Breakout, backstop, momentum filter, and
        # drawdown checks are independent of vol history and stay fully
        # live regardless, only the percentile-based regime call and the
        # vol_pause/vol_kill circuit breaker are actually blind here.
        if decision.action == RiskAction.CONTINUE and state.regime == Regime.UNKNOWN:
            decision = RiskDecision(
                action=RiskAction.PAUSE_GRID,
                reason="regime UNKNOWN: insufficient continuous vol history",
                details={"type": "regime_unknown"},
            )

        # Portfolio-level delta cap (section 9.2). This is the case
        # individual per-asset caps can't catch: both BTC and ETH pass their
        # own soft-cap checks individually but their correlated same-side
        # exposure exceeds the stricter combined budget. Only overrides
        # CONTINUE, if this asset's own checks already flagged something
        # more specific (skew, pause, flatten), that takes precedence.
        if decision.action == RiskAction.CONTINUE and self._portfolio_delta_breached(state.account_equity):
            decision = RiskDecision(
                action=RiskAction.REDUCE_ONLY,
                reason="portfolio delta cap breached across correlated assets",
                details={"type": "portfolio_delta"},
            )
        details = decision.details or {}
        state.force_reduce_only = (
            decision.action == RiskAction.REDUCE_ONLY
            and details.get("type") == "portfolio_delta"
        )

        # The desync KILL check fires on elapsed WS-stale time alone, with no
        # maintenance awareness. If the WS went stale because the last
        # reconnect hit a maintenance-pattern error (502/503/504/connection-
        # refused), treat it as a maintenance disruption rather than killing
        # the bot.
        if (
            decision.action == RiskAction.KILL
            and "desynced_seconds" in details
            and self._market_data.get_last_reconnect_error() is not None
            and self._looks_like_maintenance(self._market_data.get_last_reconnect_error())
        ):
            logger.warning(
                "Desync KILL for %s traces to a maintenance-pattern reconnect "
                "failure, entering MAINTENANCE instead: %s",
                symbol, self._market_data.get_last_reconnect_error(),
            )
            await self._handle_maintenance(symbol)
            return

        # 4. Dispatch risk action. Actions that skip all further grid/persist
        # work (KILL, CANCEL_AND_FLATTEN, PAUSE_GRID, SUPPRESS_NEW_ENTRIES)
        # return from inside the handler. SKEW_*/REDUCE_ONLY fall through so
        # GridEngine can apply the skew via state on the next reconcile.
        skip_reconcile = False
        if decision.action != RiskAction.CONTINUE:
            self._log_risk_action(symbol, decision.action, decision.reason, now_ms)
            skip_reconcile = await self._handle_risk_action(
                symbol, decision, asset_config
            )
            if state.bot_state in (BotState.DEAD, BotState.COOLDOWN, BotState.FLATTENING):
                return
        else:
            self._log_risk_action_cleared(symbol, now_ms)

        if state.bot_state not in (BotState.RUNNING, BotState.STARTING):
            return
        state.bot_state = BotState.RUNNING

        # Establish the anchor on first RANGE entry, then maintain it (re-
        # anchor when all four conditions in section 5.1 agree). Runs even
        # when skip_reconcile is True (e.g. SUPPRESS_NEW_ENTRIES) so the
        # anchor is ready the moment new entries are allowed again, rather
        # than costing an extra cycle.
        if state.regime == Regime.RANGE and vol_metrics is not None:
            await self._maintain_anchor(symbol, state, asset_config, vol_metrics, now_ms)

        # 5. Grid computation and reconciliation (unless suppressed)
        engine = self._grid_engines[symbol]
        grid_cfg = state.grid_config
        if not skip_reconcile:
            desired = engine.compute_desired_orders(state)

            if grid_cfg is not None:
                config_hash = GridEngine.compute_config_hash(
                    grid_cfg.anchor, grid_cfg.range_atr, grid_cfg.step_bps
                )
            else:
                config_hash = ""

            # Reconcile grid + backstop in a single batch (section 6.8)
            if grid_cfg is not None and state.position is not None:
                await self._order_manager.reconcile_with_backstop(
                    symbol=symbol,
                    desired=desired,
                    current=state.open_orders,
                    mid_price=state.mid_price,
                    position=state.position,
                    anchor=grid_cfg.anchor,
                    atr=vol_metrics.atr,
                    breakout_atr_distance=asset_config.breakout_atr_distance,
                    backstop_buffer_atr=asset_config.backstop_buffer_atr,
                    config_hash=config_hash,
                )
            else:
                await self._order_manager.reconcile(
                    symbol, desired, state.open_orders, state.mid_price
                )

        # 7. Persist state + grid config + pending flips
        if grid_cfg is not None:
            await self._state_store.save_grid_config(grid_cfg)
        await self._state_store.save_open_orders(symbol, state.open_orders)
        await self._state_store.save_pending_flips(symbol, state.pending_flips)
        await self._state_store.save_bot_state(symbol, state)
        await self._state_store.update_heartbeat(symbol, now_ms)

        # 8. PnL cross-check (own cadence)
        crosscheck_interval_ms = int(
            self._config.portfolio.pnl_crosscheck_interval_seconds * 1000
        )
        if now_ms - self._last_crosscheck_ms[symbol] >= crosscheck_interval_ms:
            exchange_pnl = await self._market_data.fetch_exchange_pnl(symbol)
            # None means the read was unavailable (e.g. mid-WS-reconnect) -
            # skip this cycle's cross-check rather than treating a fabricated
            # $0 as a real divergence; retry next cycle (timer not advanced).
            if exchange_pnl is not None:
                diverged = await self._pnl_monitor.crosscheck(symbol, exchange_pnl, now_ms)
                self._last_crosscheck_ms[symbol] = now_ms
                if diverged:
                    await self._send_alert(
                        "WARNING",
                        f"PnL divergence detected for {symbol}",
                    )

        # 9. REST reconciliation (own cadence)
        reconcile_interval_ms = int(
            self._config.operational.reconcile_interval_seconds * 1000
        )
        if now_ms - self._last_rest_reconcile_ms[symbol] >= reconcile_interval_ms:
            await self._rest_reconciliation(symbol)
            self._last_rest_reconcile_ms[symbol] = now_ms

        # 10. Cycle metrics (DEBUG: this fires every cycle_interval_seconds per
        # symbol, design doc 10.2's minimum-log list is discrete events
        # (regime transitions, order batches, fills, risk events, reconcile
        # discrepancies), which already log at INFO/WARNING elsewhere; this is
        # a heartbeat for troubleshooting, not one of those events)
        logger.debug(
            "cycle symbol=%s regime=%s mid=%.4f equity=%.2f orders=%d pos=%.6f",
            symbol,
            state.regime.name,
            state.mid_price,
            state.account_equity,
            len(state.open_orders),
            state.position.size if state.position else 0.0,
        )
        self._risk_manager.clear_errors()

    async def _record_equity_sample(self, state: AssetState, now_ms: int) -> None:
        """Sample exchange-reported equity for the drawdown rolling window.

        Called every cycle the bot is up, including while parked in
        COOLDOWN, same reasoning as _record_vol_sample: a planned cooldown
        shouldn't leave a hole in the 24h/168h drawdown windows. A None
        read (e.g. mid-WS-reconnect) is held rather than recorded, it would
        otherwise read as a fabricated 100% drawdown to RiskManager.
        """
        account_equity = await self._market_data.fetch_account_equity()
        if account_equity is not None:
            self._risk_manager.record_equity(now_ms, account_equity)
            state.account_equity = account_equity

    async def _record_vol_sample(self, symbol: str, now_ms: int) -> VolMetrics:
        """Sample realized vol for percentile calcs and continuity tracking.

        Called every cycle the bot is up and taking market data, including
        while parked in COOLDOWN, a planned cooldown is not a data gap.
        Only MAINTENANCE (a real exchange-side outage, where market data
        itself is unavailable) should be able to blow
        RiskManager._MAX_CONTINUOUS_GAP_MS and reset the bootstrap.
        """
        vol_metrics = self._market_data.compute_vol_metrics(symbol)
        self._risk_manager.record_vol(symbol, now_ms, vol_metrics.realized_vol)
        await self._state_store.append_vol_sample(symbol, now_ms, vol_metrics.realized_vol)
        return vol_metrics

    def _ws_is_stale(self, now_ms: int) -> bool:
        last = self._market_data.get_last_ws_message_ms()
        return last > 0 and (now_ms - last) > _WS_STALE_MS

    def _update_desync(self, now_ms: int) -> None:
        last = self._market_data.get_last_ws_message_ms()
        if last <= 0:
            return
        gap_ms = now_ms - last
        if gap_ms > _WS_STALE_MS:
            self._risk_manager.record_desync(gap_ms)
        else:
            self._risk_manager.clear_desync()

    def _portfolio_delta_breached(self, account_equity: float) -> bool:
        """Check the combined delta across all tracked assets (section 9.2)."""
        positions = {
            sym: s.position
            for sym, s in self._asset_states.items()
            if s.position is not None
        }
        if not positions:
            return False
        return self._risk_manager.check_portfolio_delta(positions, account_equity)

    # ------------------------------------------------------------------
    # Graceful shutdown (section 4.5)
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Cancel orders, persist state, disconnect, do NOT flatten."""
        logger.info("Shutdown sequence starting")

        # 1. Mark state
        for state in self._asset_states.values():
            if state.bot_state != BotState.DEAD:
                state.bot_state = BotState.SHUTTING_DOWN

        # 2. Batch cancel per asset (no flatten, section 4.5)
        for asset_cfg in self._config.assets:
            try:
                await self._order_manager.cancel_all_orders(asset_cfg.symbol)
            except Exception:
                logger.exception(
                    "Failed to cancel orders for %s during shutdown",
                    asset_cfg.symbol,
                )

        # 3. Stop fill pump
        if self._fill_task is not None and not self._fill_task.done():
            self._fill_task.cancel()
            try:
                await self._fill_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Fill pump raised during shutdown")

        # 4. Persist final state
        for symbol, state in self._asset_states.items():
            try:
                await self._state_store.save_bot_state(symbol, state)
            except Exception:
                logger.exception("Failed to persist final state for %s", symbol)

        # 5. Disconnect WS
        try:
            await self._market_data.disconnect()
        except Exception:
            logger.exception("MarketData disconnect failed")

        # 6. Close store
        try:
            await self._state_store.close()
        except Exception:
            logger.exception("StateStore close failed")

        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Risk action handling
    # ------------------------------------------------------------------

    def _log_risk_action(
        self, symbol: str, action: RiskAction, reason: str, now_ms: int,
    ) -> None:
        """Log a risk action on entry (action changed) or on a heartbeat
        cadence while it persists unchanged, not on every cycle it's
        re-evaluated as still active."""
        if action != self._last_risk_action[symbol]:
            logger.info("Risk action %s for %s: %s", action.name, symbol, reason)
            self._last_risk_action[symbol] = action
            self._risk_action_last_log_ms[symbol] = now_ms
            return

        last_log = self._risk_action_last_log_ms.get(symbol, 0)
        if now_ms - last_log >= _RISK_ACTION_HEARTBEAT_MS:
            logger.info(
                "Risk action %s for %s still active: %s", action.name, symbol, reason,
            )
            self._risk_action_last_log_ms[symbol] = now_ms

    def _log_risk_action_cleared(self, symbol: str, now_ms: int) -> None:
        """Log once when a persisted risk action clears back to CONTINUE."""
        prior = self._last_risk_action[symbol]
        if prior is not None:
            logger.info("Risk action %s for %s cleared, resuming CONTINUE", prior.name, symbol)
            self._last_risk_action[symbol] = None
            self._risk_action_last_log_ms.pop(symbol, None)

    async def _handle_risk_action(
        self,
        symbol: str,
        decision: RiskDecision,
        asset_cfg: AssetConfig,
    ) -> bool:
        """Execute the appropriate response to a risk decision.

        Returns True when the caller should skip grid reconciliation for this
        cycle (PAUSE_GRID / SUPPRESS_NEW_ENTRIES). Terminal actions
        (CANCEL_AND_FLATTEN / KILL) transition state and also return True.
        SKEW_* / REDUCE_ONLY return False, GridEngine applies the skew
        directly via state during the subsequent reconcile.
        """
        action = decision.action
        reason = decision.reason
        state = self._asset_states[symbol]

        if action in (
            RiskAction.SKEW_INVENTORY,
            RiskAction.REDUCE_ONLY,
            RiskAction.SKEW_FUNDING,
        ):
            # GridEngine reads position / funding / regime from state and
            # applies the appropriate skew during reconcile. No-op here.
            return False

        if action in (RiskAction.PAUSE_GRID, RiskAction.SUPPRESS_NEW_ENTRIES):
            # Keep existing orders in place; skip new grid reconciliation.
            return True

        if action == RiskAction.CANCEL_AND_FLATTEN:
            await self._send_alert("WARNING", f"Cancel+flatten for {symbol}: {reason}")
            await self._order_manager.cancel_all_orders(symbol)
            state.open_orders = []
            if state.position is not None and abs(state.position.size) > 0:
                state.bot_state = BotState.FLATTENING
                await self._state_store.save_bot_state(symbol, state)
                await self._run_flatten(symbol, asset_cfg)

            # If flatten failed and we're now DEAD, keep DEAD sticky -
            # do NOT overwrite with COOLDOWN.
            if state.bot_state == BotState.DEAD:
                await self._state_store.save_bot_state(symbol, state)
                return True

            # Enter cooldown
            cooldown_ms = int(asset_cfg.cooldown_minutes * 60 * 1000)
            now_ms = int(time.time() * 1000)
            state.bot_state = BotState.COOLDOWN
            state.cooldown_until_ms = now_ms + cooldown_ms
            # Only bump the breakout cooldown timer for actual breakout
            # causes; vol-kill and drawdown have their own gates and
            # shouldn't piggy-back on breakout regime-cooldown logic.
            details = decision.details or {}
            if details.get("type") in _BREAKOUT_DETAIL_TYPES:
                state.last_breakout_ms = now_ms
            await self._state_store.save_bot_state(symbol, state)
            return True

        if action == RiskAction.KILL:
            await self._send_alert("CRITICAL", f"KILL switch fired for {symbol}: {reason}")
            await self._order_manager.cancel_all_orders(symbol)
            state.open_orders = []
            if state.position is not None and abs(state.position.size) > 0:
                state.bot_state = BotState.FLATTENING
                await self._state_store.save_bot_state(symbol, state)
                await self._run_flatten(symbol, asset_cfg)
            state.bot_state = BotState.DEAD
            await self._state_store.save_bot_state(symbol, state)
            return True

        return False

    async def _maintain_anchor(
        self,
        symbol: str,
        state: AssetState,
        asset_config: AssetConfig,
        vol_metrics: VolMetrics,
        now_ms: int,
    ) -> None:
        """Establish the grid anchor if none exists yet, otherwise track
        drift and re-anchor when all four conditions agree (section 5.1).

        Nothing else in the codebase ever creates a GridConfig; without this,
        a fresh asset (or one whose persisted grid_config was never set)
        stays in RANGE indefinitely without ever placing an order, since
        GridEngine.compute_desired_orders requires a grid_config to compute
        levels against.
        """
        engine = self._grid_engines[symbol]

        if state.grid_config is None:
            state.grid_config = engine.new_grid_config(
                state.mid_price, vol_metrics, state.anchor_epoch
            )
            state.drift_start_ms = None
            logger.info(
                "Anchor established for %s at %.4f (epoch=%d)",
                symbol, state.grid_config.anchor, state.grid_config.epoch,
            )
            return

        atr = vol_metrics.atr
        if atr <= 0:
            return

        drift = abs(state.mid_price - state.grid_config.anchor)
        if drift > asset_config.anchor_shift_threshold_atr * atr:
            if state.drift_start_ms is None:
                state.drift_start_ms = now_ms
        else:
            state.drift_start_ms = None

        vol_stable = self._risk_manager.is_vol_stable_or_declining(symbol, now_ms)
        if not engine.should_reanchor(
            state.mid_price,
            state.grid_config.anchor,
            atr,
            state.regime,
            vol_stable,
            state.drift_start_ms,
            now_ms,
        ):
            return

        old_anchor = state.grid_config.anchor
        new_anchor = engine.compute_new_anchor(state.mid_price, old_anchor)
        state.anchor_epoch += 1
        state.grid_config = engine.new_grid_config(
            new_anchor, vol_metrics, state.anchor_epoch
        )
        state.drift_start_ms = None
        # Restart staggered deployment against the new anchor rather than
        # instantly placing every level at the freshly re-centered grid.
        state.stagger_placed_count = 0
        logger.info(
            "Re-anchored %s: %.4f -> %.4f (epoch=%d)",
            symbol, old_anchor, new_anchor, state.anchor_epoch,
        )
        await self._send_alert(
            "INFO", f"{symbol} re-anchored: {old_anchor:.4f} -> {new_anchor:.4f}"
        )

    async def _run_flatten(self, symbol: str, asset_cfg: AssetConfig) -> None:
        """Invoke OrderManager's flatten state machine (section 6.7)."""
        state = self._asset_states[symbol]
        if state.position is None or abs(state.position.size) < 1e-12:
            return

        async def _get_mid(sym: str) -> float:
            return self._market_data.get_mid_price(sym)

        async def _get_depth(sym: str, slippage_bps: float, side) -> float:
            return await self._market_data.fetch_book_depth(sym, slippage_bps, side)

        async def _get_position(sym: str) -> Position | None:
            return await self._market_data.fetch_position(sym)

        fully_flattened = await self._order_manager.execute_flatten(
            symbol,
            state.position,
            asset_cfg,
            _get_mid,
            _get_depth,
            _get_position,
        )
        if not fully_flattened:
            logger.error("Flatten incomplete for %s, entering DEAD", symbol)
            state.bot_state = BotState.DEAD
            await self._send_alert("CRITICAL", f"Flatten residual for {symbol}")
        # Refresh position post-flatten
        state.position = await self._market_data.fetch_position(symbol)

    # ------------------------------------------------------------------
    # REST reconciliation (section 4.3, backup path)
    # ------------------------------------------------------------------

    async def _rest_reconciliation(self, symbol: str) -> None:
        """Periodic REST consistency check against WS-maintained state."""
        try:
            rest_orders = await self._market_data.fetch_open_orders(symbol)
            rest_position = await self._market_data.fetch_position(symbol)
        except Exception:
            logger.exception("REST reconciliation failed for %s", symbol)
            self._risk_manager.record_error()
            return

        state = self._asset_states[symbol]

        local_cloids = {o.client_order_id for o in state.open_orders}
        rest_cloids = {o.client_order_id for o in rest_orders}
        if local_cloids != rest_cloids:
            logger.warning(
                "REST/WS order divergence for %s: local=%d rest=%d, adopting exchange",
                symbol, len(local_cloids), len(rest_cloids),
            )
            # Design section 10.3: "Reconcile discrepancy detected" is a
            # High-severity alert, not just a log line, the operator needs
            # to know WS silently dropped an event even though REST self-healed
            # it. This module maps design's High tier to WARNING, matching
            # the existing convention used for breakout-flatten alerts below.
            await self._send_alert(
                "WARNING",
                f"REST/WS order divergence for {symbol}: local={len(local_cloids)} "
                f"rest={len(rest_cloids)}, adopted exchange state",
            )
            state.open_orders = rest_orders

        # Position reconciliation
        local_size = state.position.size if state.position else 0.0
        rest_size = rest_position.size if rest_position else 0.0
        if abs(local_size - rest_size) > 1e-9:
            logger.warning(
                "REST/WS position divergence for %s: local=%.8f rest=%.8f, adopting exchange",
                symbol, local_size, rest_size,
            )
            await self._send_alert(
                "WARNING",
                f"REST/WS position divergence for {symbol}: local={local_size:.8f} "
                f"rest={rest_size:.8f}, adopted exchange state",
            )
        state.position = rest_position

        self._risk_manager.clear_desync()

    # ------------------------------------------------------------------
    # Maintenance detection (section 10.4)
    # ------------------------------------------------------------------

    async def _handle_maintenance(self, symbol: str) -> None:
        """Enter maintenance-aware mode for all assets.

        MarketData handles the WS reconnect with exponential backoff
        internally; supervisor's job is to stop counting errors toward the
        kill switch and avoid risk decisions during the window.
        """
        logger.warning("Entering MAINTENANCE mode")
        if self._maintenance_entered_ms is None:
            self._maintenance_entered_ms = int(time.time() * 1000)
            await self._send_alert(
                "INFO", f"Exchange maintenance detected (triggered by {symbol})"
            )
        for state in self._asset_states.values():
            if state.bot_state not in (BotState.DEAD, BotState.SHUTTING_DOWN):
                state.bot_state = BotState.MAINTENANCE
        self._risk_manager.record_error(is_maintenance=True)

    async def _log_maintenance_duration_if_done(self, now_ms: int) -> None:
        """Log total MAINTENANCE episode duration once every asset has exited.

        Assets can exit MAINTENANCE on different cycles, so this only fires
        (and clears the entry timestamp) once none remain in that state -
        otherwise the first asset to reconcile would log a partial duration.
        """
        if self._maintenance_entered_ms is None:
            return
        if any(
            state.bot_state == BotState.MAINTENANCE
            for state in self._asset_states.values()
        ):
            return
        duration_s = (now_ms - self._maintenance_entered_ms) / 1000.0
        logger.warning("MAINTENANCE episode ended after %.1fs", duration_s)
        await self._send_alert(
            "INFO", f"Exchange maintenance ended after {duration_s:.1f}s"
        )
        self._maintenance_entered_ms = None

    # ------------------------------------------------------------------
    # Alerting (section 10.3)
    # ------------------------------------------------------------------

    async def _send_alert(self, severity: str, message: str) -> None:
        """Send alert via configured channel (pluggable callback)."""
        level = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "CRITICAL": logging.CRITICAL,
        }.get(severity.upper(), logging.INFO)
        logger.log(level, "[ALERT:%s] %s", severity, message)

        if self._alert_callback is not None:
            try:
                await self._alert_callback(severity, message)
            except Exception:
                logger.exception("Alert callback failed")

    # ------------------------------------------------------------------
    # Fill event processing (section 7.12)
    # ------------------------------------------------------------------

    async def _fill_pump(self) -> None:
        """Drain MarketData fills queue and route to handlers."""
        queue = self._market_data.fills
        if queue is None:
            return
        while not self._shutdown_requested:
            try:
                fill = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._route_fill(fill)
            except Exception:
                logger.exception("Failed to route fill %s", fill.fill_id)

    async def _route_fill(self, fill: Fill) -> None:
        """Route a fill to PnLMonitor, OrderManager flip logic, StateStore."""
        symbol = fill.symbol
        state = self._asset_states.get(symbol)

        logger.info(
            "Fill %s: %s %s %.6f @ %.2f fee=%.6f%s%s",
            fill.fill_id,
            symbol,
            fill.side.value,
            fill.size,
            fill.price,
            fill.fee,
            " maker" if fill.is_maker else " taker",
            " partial" if fill.is_partial else "",
        )

        if state is None:
            return

        # PnL ledger
        self._pnl_monitor.record_fill(fill)

        # StateStore ledger
        await self._state_store.record_fill(fill)

        # Only flip on full fills
        if fill.is_partial or state.grid_config is None:
            return

        position_size = state.position.size if state.position else 0.0
        zone = self._grid_engines[symbol].classify_inventory_zone(position_size)
        flip = self._order_manager.compute_flip_order(
            fill,
            state.grid_config.step_bps,
            inventory_zone_is_hard_cap=(zone == InventoryZone.HARD_CAP),
        )
        if flip is None:
            return

        # Persist the flip as a pending flip (section 7.6)
        state.pending_flips.append(
            PendingFlip(
                price=flip.price,
                side=flip.side,
                size=flip.size,
                originating_fill_id=fill.fill_id,
            )
        )
        await self._state_store.save_pending_flips(symbol, state.pending_flips)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _asset_config(self, symbol: str) -> AssetConfig:
        for ac in self._config.assets:
            if ac.symbol == symbol:
                return ac
        return self._config.assets[0]

    @staticmethod
    def _looks_like_maintenance(exc: BaseException) -> bool:
        """Classify whether an exception is from exchange maintenance.

        Matching is deliberately conservative: type-based first, then
        substring on the message for SDK-wrapped HTTP errors that can't be
        type-checked directly.
        """
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        msg = str(exc).lower()
        return any(
            token in msg
            for token in (
                "503", "service unavailable",
                "502", "bad gateway",
                "504", "gateway timeout",
                "connection refused", "maintenance",
            )
        )
