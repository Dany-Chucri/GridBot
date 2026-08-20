"""Tests for Supervisor — orchestration, cycle flow, risk routing, shutdown."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from hyperliquid.utils.error import ServerError

from gridbot.config import AssetConfig, BotConfig, OperationalConfig, PortfolioConfig
from gridbot.risk_manager import RiskAction, RiskDecision
from gridbot.supervisor import Supervisor
from gridbot.types import (
    AssetState,
    BotState,
    Fill,
    GridConfig,
    OpenOrder,
    OrderSide,
    PendingFlip,
    Position,
    Regime,
    VolMetrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(symbols: tuple[str, ...] = ("BTC-PERP",)) -> BotConfig:
    assets = [AssetConfig(symbol=s, max_abs_position=1.0) for s in symbols]
    return BotConfig(
        assets=assets,
        operational=OperationalConfig(
            cycle_interval_seconds=0.01,
            reconcile_interval_seconds=0.02,
        ),
        portfolio=PortfolioConfig(pnl_crosscheck_interval_seconds=0.05),
    )


def _make_supervisor(
    config: BotConfig | None = None,
    *,
    market_data=None,
    order_manager=None,
    risk_manager=None,
    state_store=None,
    pnl_monitor=None,
) -> Supervisor:
    cfg = config or _cfg()
    return Supervisor(
        cfg,
        market_data=market_data or _mock_market_data(),
        order_manager=order_manager or _mock_order_manager(),
        risk_manager=risk_manager or _mock_risk_manager(),
        state_store=state_store or _mock_state_store(),
        pnl_monitor=pnl_monitor or _mock_pnl_monitor(),
    )


def _mock_market_data(mid: float = 50000.0) -> MagicMock:
    md = MagicMock()
    md.connect = AsyncMock()
    md.disconnect = AsyncMock()
    md.fetch_account_equity = AsyncMock(return_value=100_000.0)
    md.fetch_open_orders = AsyncMock(return_value=[])
    md.fetch_position = AsyncMock(return_value=None)
    md.fetch_fills = AsyncMock(return_value=[])
    md.fetch_exchange_pnl = AsyncMock(return_value=0.0)
    md.fetch_book_depth = AsyncMock(return_value=10.0)
    md.get_mid_price = MagicMock(return_value=mid)
    md.get_mark_price = MagicMock(return_value=mid)
    md.get_funding_rate = MagicMock(return_value=0.0)
    md.get_moving_average = MagicMock(return_value=0.0)
    md.get_last_ws_message_ms = MagicMock(return_value=0)
    md.is_ws_connected = MagicMock(return_value=True)
    md.get_last_reconnect_error = MagicMock(return_value=None)
    md.compute_vol_metrics = MagicMock(return_value=VolMetrics(
        realized_vol=0.5,
        atr=100.0,
        spread_bps=2.0,
        rolling_return_1m=0.0,
        rolling_return_5m=0.0,
    ))
    md.fills = asyncio.Queue()
    return md


def _mock_order_manager() -> MagicMock:
    om = MagicMock()
    om.initialize = AsyncMock()
    om.reconcile = AsyncMock()
    om.reconcile_with_backstop = AsyncMock()
    om.cancel_all_orders = AsyncMock()
    om.cancel_orders = AsyncMock()
    om.execute_flatten = AsyncMock(return_value=True)
    om.update_backstop = AsyncMock()
    om.compute_flip_order = MagicMock(return_value=None)
    return om


def _mock_risk_manager(
    action: RiskAction = RiskAction.CONTINUE, reason: str = "ok"
) -> MagicMock:
    rm = MagicMock()
    rm.evaluate = MagicMock(return_value=RiskDecision(action=action, reason=reason))
    rm.detect_regime = MagicMock(return_value=Regime.RANGE)
    rm.preflight_check = MagicMock(return_value=[])
    rm.record_equity = MagicMock()
    rm.record_vol = MagicMock()
    rm.load_vol_history = MagicMock()
    rm.record_error = MagicMock()
    rm.record_desync = MagicMock()
    rm.clear_errors = MagicMock()
    rm.clear_desync = MagicMock()
    return rm


def _mock_state_store() -> MagicMock:
    ss = MagicMock()
    ss.initialize = AsyncMock()
    ss.close = AsyncMock()
    ss.load_bot_state = AsyncMock(return_value=None)
    ss.load_grid_config = AsyncMock(return_value=None)
    ss.load_pending_flips = AsyncMock(return_value=[])
    ss.load_vol_history = AsyncMock(return_value=[])
    ss.append_vol_sample = AsyncMock()
    ss.save_grid_config = AsyncMock()
    ss.save_open_orders = AsyncMock()
    ss.save_pending_flips = AsyncMock()
    ss.save_bot_state = AsyncMock()
    ss.update_heartbeat = AsyncMock()
    ss.get_last_heartbeat = AsyncMock(return_value=None)
    ss.record_fill = AsyncMock()
    return ss


def _mock_pnl_monitor() -> MagicMock:
    pm = MagicMock()
    pm.record_fill = MagicMock()
    pm.crosscheck = AsyncMock(return_value=False)
    return pm


def _fill(
    symbol: str = "BTC-PERP",
    price: float = 50000.0,
    size: float = 0.1,
    side: OrderSide = OrderSide.BUY,
    is_partial: bool = False,
) -> Fill:
    return Fill(
        fill_id=f"f-{price}-{side.value}",
        order_id=1,
        client_order_id="cloid",
        symbol=symbol,
        price=price,
        size=size,
        side=side,
        fee=0.0,
        timestamp_ms=1000,
        is_maker=True,
        is_partial=is_partial,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_creates_engines_and_states_per_asset(self):
        sup = _make_supervisor(_cfg(("BTC-PERP", "ETH-PERP")))
        assert set(sup._grid_engines.keys()) == {"BTC-PERP", "ETH-PERP"}
        assert set(sup._asset_states.keys()) == {"BTC-PERP", "ETH-PERP"}

    def test_request_shutdown_sets_flag(self):
        sup = _make_supervisor()
        assert sup._shutdown_requested is False
        sup.request_shutdown()
        assert sup._shutdown_requested is True


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initializes_all_modules(self):
        md = _mock_market_data()
        om = _mock_order_manager()
        ss = _mock_state_store()
        sup = _make_supervisor(market_data=md, order_manager=om, state_store=ss)

        await sup._initialize()

        ss.initialize.assert_awaited_once()
        om.initialize.assert_awaited_once()
        md.connect.assert_awaited_once()
        # Fill pump started
        assert sup._fill_task is not None
        sup._fill_task.cancel()

    @pytest.mark.asyncio
    async def test_raises_when_ws_never_delivers_price(self, monkeypatch):
        md = _mock_market_data(mid=0.0)
        sup = _make_supervisor(market_data=md)

        # Patch the module-level timeout to something short for the test
        monkeypatch.setattr("gridbot.supervisor._INITIAL_WS_TIMEOUT_S", 0.2)

        with pytest.raises(RuntimeError, match="Timed out"):
            await sup._initialize()


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


class TestPreflight:
    @pytest.mark.asyncio
    async def test_refuses_on_violation(self):
        rm = _mock_risk_manager()
        rm.preflight_check = MagicMock(return_value=["too much leverage"])
        sup = _make_supervisor(risk_manager=rm)

        with pytest.raises(RuntimeError, match="Pre-flight checks failed"):
            await sup._preflight_checks()

    @pytest.mark.asyncio
    async def test_refuses_on_zero_equity(self):
        md = _mock_market_data()
        md.fetch_account_equity = AsyncMock(return_value=0.0)
        sup = _make_supervisor(market_data=md)

        with pytest.raises(RuntimeError, match="non-positive"):
            await sup._preflight_checks()

    @pytest.mark.asyncio
    async def test_transitions_assets_to_running_on_pass(self):
        sup = _make_supervisor()
        await sup._preflight_checks()
        for state in sup._asset_states.values():
            assert state.bot_state == BotState.RUNNING


# ---------------------------------------------------------------------------
# State recovery
# ---------------------------------------------------------------------------


class TestRecovery:
    @pytest.mark.asyncio
    async def test_restores_persisted_state(self):
        persisted = AssetState(symbol="BTC-PERP", bot_state=BotState.RUNNING)
        ss = _mock_state_store()
        ss.load_bot_state = AsyncMock(return_value=persisted)
        sup = _make_supervisor(state_store=ss)

        await sup._recover_state()
        # Bot state is reset to STARTING for fresh run
        assert sup._asset_states["BTC-PERP"].bot_state == BotState.STARTING

    @pytest.mark.asyncio
    async def test_recovery_reloads_vol_history(self):
        """A restart must not lose real, already-accumulated vol history —
        it's loaded from the store and fed to RiskManager so a restart
        doesn't cost a fresh 48h bootstrap on top of genuine data."""
        samples = [(1000, 0.25), (2000, 0.30)]
        ss = _mock_state_store()
        ss.load_vol_history = AsyncMock(return_value=samples)
        rm = _mock_risk_manager()
        sup = _make_supervisor(state_store=ss, risk_manager=rm)

        await sup._recover_state()

        ss.load_vol_history.assert_awaited_once_with("BTC-PERP")
        rm.load_vol_history.assert_called_once()
        args, kwargs = rm.load_vol_history.call_args
        assert args[0] == "BTC-PERP"
        assert args[1] == samples

    @pytest.mark.asyncio
    async def test_cancels_orphan_orders(self):
        orphan = OpenOrder(
            order_id=42,
            client_order_id="0xunknown",
            symbol="BTC-PERP",
            price=49000.0,
            size=0.1,
            remaining=0.1,
            side=OrderSide.BUY,
        )
        md = _mock_market_data()
        md.fetch_open_orders = AsyncMock(side_effect=[[orphan], []])
        om = _mock_order_manager()
        sup = _make_supervisor(market_data=md, order_manager=om)

        await sup._recover_state()

        om.cancel_orders.assert_awaited_once()
        args, kwargs = om.cancel_orders.await_args
        assert args[0] == "BTC-PERP"
        assert len(args[1]) == 1
        assert args[1][0].client_order_id == "0xunknown"

    @pytest.mark.asyncio
    async def test_routes_missed_fill_for_order_that_filled_while_down(self):
        """Design section 4.4 step 4: an order in local state but missing
        from the exchange snapshot must be checked against the fills
        endpoint — if it filled while the bot was down, that fill must
        still be routed to PnLMonitor/StateStore/flip logic, not dropped."""
        symbol = "BTC-PERP"
        vanished_order = OpenOrder(
            order_id=55, client_order_id="0xvanished" + "0" * 24,
            symbol=symbol, price=49500.0, size=0.1, remaining=0.1,
            side=OrderSide.BUY,
        )
        persisted = AssetState(
            symbol=symbol, bot_state=BotState.RUNNING,
            open_orders=[vanished_order],
            grid_config=GridConfig(
                symbol=symbol, anchor=50000.0, range_atr=2.5, step_bps=20.0, epoch=1,
            ),
        )
        ss = _mock_state_store()
        ss.load_bot_state = AsyncMock(return_value=persisted)

        md = _mock_market_data()
        md.fetch_open_orders = AsyncMock(return_value=[])  # order no longer resting
        md.fetch_position = AsyncMock(return_value=Position(
            symbol=symbol, size=0.1, avg_entry_price=49500.0, unrealized_pnl=0.0,
        ))
        md.fetch_fills = AsyncMock(return_value=[Fill(
            fill_id="0xh1", order_id=55, client_order_id="",
            symbol=symbol, price=49500.0, size=0.1, side=OrderSide.BUY,
            fee=0.5, timestamp_ms=999, is_maker=True, is_partial=False,
        )])

        om = _mock_order_manager()
        pm = _mock_pnl_monitor()
        sup = _make_supervisor(market_data=md, order_manager=om, state_store=ss, pnl_monitor=pm)

        await sup._recover_state()

        pm.record_fill.assert_called_once()
        routed_fill = pm.record_fill.call_args[0][0]
        assert routed_fill.order_id == 55
        assert routed_fill.client_order_id == "0xvanished" + "0" * 24
        ss.record_fill.assert_awaited_once()
        om.compute_flip_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_fill_match_for_vanished_order_is_a_noop(self):
        """A vanished order with no matching fill was simply cancelled —
        no fill should be synthesized/routed."""
        symbol = "BTC-PERP"
        vanished_order = OpenOrder(
            order_id=55, client_order_id="0xvanished" + "0" * 24,
            symbol=symbol, price=49500.0, size=0.1, remaining=0.1,
            side=OrderSide.BUY,
        )
        persisted = AssetState(
            symbol=symbol, bot_state=BotState.RUNNING,
            open_orders=[vanished_order],
        )
        ss = _mock_state_store()
        ss.load_bot_state = AsyncMock(return_value=persisted)

        md = _mock_market_data()
        md.fetch_open_orders = AsyncMock(return_value=[])
        md.fetch_fills = AsyncMock(return_value=[])  # nothing filled — was cancelled

        pm = _mock_pnl_monitor()
        sup = _make_supervisor(market_data=md, state_store=ss, pnl_monitor=pm)

        await sup._recover_state()

        pm.record_fill.assert_not_called()

    @pytest.mark.asyncio
    async def test_resumes_flattening_when_position_remains(self):
        persisted = AssetState(
            symbol="BTC-PERP", bot_state=BotState.FLATTENING,
        )
        ss = _mock_state_store()
        ss.load_bot_state = AsyncMock(return_value=persisted)

        md = _mock_market_data()
        md.fetch_position = AsyncMock(return_value=Position(
            symbol="BTC-PERP", size=0.5, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        ))
        om = _mock_order_manager()
        sup = _make_supervisor(market_data=md, order_manager=om, state_store=ss)

        await sup._recover_state()
        om.execute_flatten.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


class TestCycle:
    @pytest.mark.asyncio
    async def test_cycle_calls_modules_in_order(self):
        md = _mock_market_data()
        rm = _mock_risk_manager()
        om = _mock_order_manager()
        ss = _mock_state_store()
        sup = _make_supervisor(
            market_data=md, order_manager=om, risk_manager=rm, state_store=ss
        )

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        md.fetch_account_equity.assert_awaited_once()
        rm.record_equity.assert_called_once()
        rm.detect_regime.assert_called_once()
        rm.evaluate.assert_called_once()
        # No grid_config -> fallback reconcile path (no position yet)
        om.reconcile.assert_awaited_once()
        ss.save_bot_state.assert_awaited()
        ss.update_heartbeat.assert_awaited()

    @pytest.mark.asyncio
    async def test_cycle_holds_last_equity_when_fetch_unavailable(self):
        """Regression (2026-08-18 false-KILL incident): a None equity read
        (e.g. mid-WS-reconnect, when MarketData._info is torn down) must not
        overwrite state.account_equity with a fabricated $0 — that reads as
        a 100% drawdown to RiskManager and trips a false KILL. The cycle
        should hold the prior equity and skip recording an equity sample."""
        md = _mock_market_data()
        md.fetch_account_equity = AsyncMock(return_value=None)
        rm = _mock_risk_manager()
        sup = _make_supervisor(market_data=md, risk_manager=rm)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING
        state.account_equity = 100_000.0

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        assert state.account_equity == 100_000.0
        rm.record_equity.assert_not_called()

    @pytest.mark.asyncio
    async def test_cycle_skips_pnl_crosscheck_when_exchange_pnl_unavailable(self):
        """Regression, same class as the equity fix above: a None PnL read
        (mid-WS-reconnect) must not be treated as a real $0 divergence signal
        — skip the cross-check for this cycle and retry next cycle."""
        md = _mock_market_data()
        md.fetch_exchange_pnl = AsyncMock(return_value=None)
        pm = _mock_pnl_monitor()
        sup = _make_supervisor(market_data=md, pnl_monitor=pm)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        pm.crosscheck.assert_not_called()
        assert sup._last_crosscheck_ms[asset_cfg.symbol] == 0

    @pytest.mark.asyncio
    async def test_cycle_uses_reconcile_with_backstop_when_position_exists(self):
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING
        state.grid_config = GridConfig(
            symbol=asset_cfg.symbol, anchor=50000.0,
            range_atr=2.5, step_bps=20.0, epoch=1,
        )
        state.position = Position(
            symbol=asset_cfg.symbol, size=0.1,
            avg_entry_price=50000.0, unrealized_pnl=0.0,
        )

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)
        om.reconcile_with_backstop.assert_awaited_once()
        om.reconcile.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trend_regime_flattens_and_enters_cooldown(self):
        """Design section 8.1: TREND regime must cancel orders, flatten any
        inventory, and enter cooldown — even when RiskManager.evaluate()
        itself returns CONTINUE (e.g. a slow grind away from the moving
        average that hasn't crossed the breakout-distance or vol-spike
        thresholds evaluate() checks independently)."""
        rm = _mock_risk_manager()
        rm.detect_regime = MagicMock(return_value=Regime.TREND)
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, order_manager=om)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING
        state.position = Position(
            symbol=asset_cfg.symbol, size=0.2,
            avg_entry_price=50000.0, unrealized_pnl=0.0,
        )

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        om.cancel_all_orders.assert_awaited()
        om.execute_flatten.assert_awaited()
        assert state.bot_state == BotState.COOLDOWN
        assert state.cooldown_until_ms is not None
        # Not a breakout-type cause — shouldn't bump the breakout cooldown timer
        assert state.last_breakout_ms is None

    @pytest.mark.asyncio
    async def test_trend_regime_does_not_override_more_severe_action(self):
        """A CANCEL_AND_FLATTEN/KILL from evaluate() (e.g. drawdown) must not
        be masked by the regime-TREND override — only CONTINUE is eligible."""
        rm = _mock_risk_manager(action=RiskAction.KILL, reason="drawdown")
        rm.detect_regime = MagicMock(return_value=Regime.TREND)
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, order_manager=om)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        assert state.bot_state == BotState.DEAD

    @pytest.mark.asyncio
    async def test_unknown_regime_pauses_grid_without_cancelling_orders(self):
        """UNKNOWN (insufficient continuous vol history) must not fall
        through to a real reconcile — GridEngine.compute_desired_orders
        returns [] for any non-RANGE regime, and diffing that against real
        resting orders would cancel all of them, including pending flips.
        UNKNOWN gets the same PAUSE_GRID treatment as HIGH_VOL: pause new
        placement, leave existing orders resting."""
        rm = _mock_risk_manager()
        rm.detect_regime = MagicMock(return_value=Regime.UNKNOWN)
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, order_manager=om)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING
        state.position = Position(
            symbol=asset_cfg.symbol, size=0.2,
            avg_entry_price=50000.0, unrealized_pnl=0.0,
        )

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        om.reconcile.assert_not_awaited()
        om.reconcile_with_backstop.assert_not_awaited()
        om.cancel_all_orders.assert_not_awaited()
        assert state.bot_state == BotState.RUNNING

    @pytest.mark.asyncio
    async def test_unknown_regime_does_not_override_more_severe_action(self):
        """A CANCEL_AND_FLATTEN/KILL from evaluate() must not be masked by
        the regime-UNKNOWN override — only CONTINUE is eligible."""
        rm = _mock_risk_manager(action=RiskAction.KILL, reason="drawdown")
        rm.detect_regime = MagicMock(return_value=Regime.UNKNOWN)
        sup = _make_supervisor(risk_manager=rm)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        assert state.bot_state == BotState.DEAD

    @pytest.mark.asyncio
    async def test_portfolio_delta_breach_forces_reduce_only(self):
        """Design section 9.2: a portfolio-level delta breach across
        correlated assets must force reduce-only behavior even when this
        asset's own per-asset checks all pass (evaluate() -> CONTINUE)."""
        rm = _mock_risk_manager()
        rm.check_portfolio_delta = MagicMock(return_value=True)
        sup = _make_supervisor(risk_manager=rm)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING
        state.position = Position(
            symbol=asset_cfg.symbol, size=0.1,
            avg_entry_price=50000.0, unrealized_pnl=0.0,
        )

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        rm.check_portfolio_delta.assert_called_once()
        assert state.force_reduce_only is True

    @pytest.mark.asyncio
    async def test_portfolio_delta_ok_leaves_reduce_only_unset(self):
        rm = _mock_risk_manager()
        rm.check_portfolio_delta = MagicMock(return_value=False)
        sup = _make_supervisor(risk_manager=rm)

        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.RUNNING

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        assert state.force_reduce_only is False

    @pytest.mark.asyncio
    async def test_cooldown_respected(self):
        sup = _make_supervisor()
        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[asset_cfg.symbol]
        state.bot_state = BotState.COOLDOWN
        state.cooldown_until_ms = 2**62  # far future

        await sup._run_cycle(asset_cfg.symbol, asset_cfg)
        # No modules advanced past the cooldown gate
        # RiskManager.evaluate should NOT be called
        sup._risk_manager.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# Risk action handling
# ---------------------------------------------------------------------------


class TestRiskActionLogThrottling:
    """Risk actions log on entry, on a 10-minute heartbeat while unchanged,
    and once on exit — not every cycle a persistent condition is re-evaluated
    (regression: SUPPRESS_NEW_ENTRIES was logging every cycle_interval)."""

    def test_logs_once_on_entry_not_on_repeat(self, caplog):
        sup = _make_supervisor()
        symbol = sup._config.assets[0].symbol

        with caplog.at_level("INFO"):
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 1_000)
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 2_000)
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 3_000)

        risk_lines = [r.message for r in caplog.records if "Risk action" in r.message]
        assert len(risk_lines) == 1

    def test_heartbeats_after_ten_minutes(self, caplog):
        sup = _make_supervisor()
        symbol = sup._config.assets[0].symbol

        with caplog.at_level("INFO"):
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 0)
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 9 * 60 * 1000)
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 11 * 60 * 1000)

        risk_lines = [r.message for r in caplog.records if "Risk action" in r.message]
        assert len(risk_lines) == 2
        assert "still active" in risk_lines[1]

    def test_logs_on_clear(self, caplog):
        sup = _make_supervisor()
        symbol = sup._config.assets[0].symbol

        with caplog.at_level("INFO"):
            sup._log_risk_action(symbol, RiskAction.SUPPRESS_NEW_ENTRIES, "momentum", 0)
            sup._log_risk_action_cleared(symbol, 1_000)
            sup._log_risk_action_cleared(symbol, 2_000)  # already clear — no repeat

        cleared_lines = [r.message for r in caplog.records if "cleared" in r.message]
        assert len(cleared_lines) == 1
        assert sup._last_risk_action[symbol] is None


class TestRiskActions:
    @pytest.mark.asyncio
    async def test_kill_cancels_and_enters_dead(self):
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[symbol]
        state.position = Position(
            symbol=symbol, size=0.5, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        )

        await sup._handle_risk_action(
            symbol, RiskDecision(action=RiskAction.KILL, reason="vol kill"), asset_cfg
        )

        om.cancel_all_orders.assert_awaited()
        om.execute_flatten.assert_awaited()
        assert state.bot_state == BotState.DEAD

    @pytest.mark.asyncio
    async def test_cancel_and_flatten_enters_cooldown(self):
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[symbol]
        state.position = Position(
            symbol=symbol, size=0.5, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        )

        await sup._handle_risk_action(
            symbol,
            RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="breakout",
                details={"type": "distance"},
            ),
            asset_cfg,
        )

        om.cancel_all_orders.assert_awaited()
        om.execute_flatten.assert_awaited()
        assert state.bot_state == BotState.COOLDOWN
        assert state.cooldown_until_ms is not None
        assert state.last_breakout_ms is not None

    @pytest.mark.asyncio
    async def test_cancel_and_flatten_does_not_set_breakout_for_non_breakout(self):
        """vol-kill and drawdown shouldn't piggy-back on breakout cooldown."""
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[symbol]
        state.position = None

        await sup._handle_risk_action(
            symbol,
            RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="vol kill",
                details={"percentile": 99.0},
            ),
            asset_cfg,
        )
        assert state.bot_state == BotState.COOLDOWN
        assert state.last_breakout_ms is None

    @pytest.mark.asyncio
    async def test_cancel_and_flatten_dead_stays_dead(self):
        """If flatten fails and sets DEAD, COOLDOWN must not overwrite."""
        om = _mock_order_manager()
        om.execute_flatten = AsyncMock(return_value=False)  # flatten residual
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]
        state = sup._asset_states[symbol]
        state.position = Position(
            symbol=symbol, size=0.5, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        )

        await sup._handle_risk_action(
            symbol,
            RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="breakout",
                details={"type": "distance"},
            ),
            asset_cfg,
        )
        assert state.bot_state == BotState.DEAD

    @pytest.mark.asyncio
    async def test_pause_grid_does_not_cancel(self):
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]

        skip = await sup._handle_risk_action(
            symbol,
            RiskDecision(action=RiskAction.PAUSE_GRID, reason="high vol"),
            asset_cfg,
        )
        om.cancel_all_orders.assert_not_awaited()
        om.execute_flatten.assert_not_awaited()
        assert skip is True

    @pytest.mark.asyncio
    async def test_skew_inventory_is_noop(self):
        om = _mock_order_manager()
        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        asset_cfg = sup._config.assets[0]

        skip = await sup._handle_risk_action(
            symbol,
            RiskDecision(action=RiskAction.SKEW_INVENTORY, reason="soft cap"),
            asset_cfg,
        )
        om.cancel_all_orders.assert_not_awaited()
        # Skew must NOT skip reconcile — GridEngine needs to apply the skew.
        assert skip is False


# ---------------------------------------------------------------------------
# REST reconciliation
# ---------------------------------------------------------------------------


class TestRestReconciliation:
    @pytest.mark.asyncio
    async def test_adopts_exchange_orders_on_divergence(self):
        symbol = "BTC-PERP"
        rest_order = OpenOrder(
            order_id=1, client_order_id="0xabc", symbol=symbol,
            price=49000.0, size=0.1, remaining=0.1, side=OrderSide.BUY,
        )
        md = _mock_market_data()
        md.fetch_open_orders = AsyncMock(return_value=[rest_order])
        sup = _make_supervisor(market_data=md)

        state = sup._asset_states[symbol]
        state.open_orders = []  # local thinks no orders

        await sup._rest_reconciliation(symbol)
        assert state.open_orders == [rest_order]

    @pytest.mark.asyncio
    async def test_adopts_exchange_position(self):
        symbol = "BTC-PERP"
        rest_pos = Position(
            symbol=symbol, size=0.3, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        )
        md = _mock_market_data()
        md.fetch_position = AsyncMock(return_value=rest_pos)
        sup = _make_supervisor(market_data=md)

        await sup._rest_reconciliation(symbol)
        assert sup._asset_states[symbol].position == rest_pos

    @pytest.mark.asyncio
    async def test_order_divergence_sends_alert(self):
        """Design section 10.3: reconcile discrepancy must alert, not just log."""
        symbol = "BTC-PERP"
        rest_order = OpenOrder(
            order_id=1, client_order_id="0xabc", symbol=symbol,
            price=49000.0, size=0.1, remaining=0.1, side=OrderSide.BUY,
        )
        md = _mock_market_data()
        md.fetch_open_orders = AsyncMock(return_value=[rest_order])
        sup = _make_supervisor(market_data=md)
        sup._asset_states[symbol].open_orders = []

        alerts = []
        sup._alert_callback = AsyncMock(side_effect=lambda sev, msg: alerts.append((sev, msg)))

        await sup._rest_reconciliation(symbol)

        assert any("divergence" in msg for _, msg in alerts)

    @pytest.mark.asyncio
    async def test_position_divergence_sends_alert(self):
        symbol = "BTC-PERP"
        rest_pos = Position(
            symbol=symbol, size=0.3, avg_entry_price=50000.0,
            unrealized_pnl=0.0,
        )
        md = _mock_market_data()
        md.fetch_position = AsyncMock(return_value=rest_pos)
        sup = _make_supervisor(market_data=md)

        alerts = []
        sup._alert_callback = AsyncMock(side_effect=lambda sev, msg: alerts.append((sev, msg)))

        await sup._rest_reconciliation(symbol)

        assert any("divergence" in msg for _, msg in alerts)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_cancels_without_flatten(self):
        om = _mock_order_manager()
        md = _mock_market_data()
        ss = _mock_state_store()
        sup = _make_supervisor(order_manager=om, market_data=md, state_store=ss)

        await sup._shutdown()

        om.cancel_all_orders.assert_awaited()
        # Design: no flatten on graceful shutdown
        om.execute_flatten.assert_not_awaited()
        md.disconnect.assert_awaited()
        ss.close.assert_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_marks_assets(self):
        sup = _make_supervisor()
        await sup._shutdown()
        for state in sup._asset_states.values():
            assert state.bot_state == BotState.SHUTTING_DOWN


# ---------------------------------------------------------------------------
# Fill routing
# ---------------------------------------------------------------------------


class TestFillRouting:
    @pytest.mark.asyncio
    async def test_partial_fill_records_but_no_flip(self):
        pm = _mock_pnl_monitor()
        om = _mock_order_manager()
        ss = _mock_state_store()
        sup = _make_supervisor(pnl_monitor=pm, order_manager=om, state_store=ss)

        symbol = sup._config.assets[0].symbol
        sup._asset_states[symbol].grid_config = GridConfig(
            symbol=symbol, anchor=50000.0, range_atr=2.5, step_bps=20.0, epoch=1,
        )

        await sup._route_fill(_fill(is_partial=True))
        pm.record_fill.assert_called_once()
        ss.record_fill.assert_awaited_once()
        om.compute_flip_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_fill_creates_pending_flip(self):
        om = _mock_order_manager()
        # compute_flip_order returns a valid DesiredOrder
        from gridbot.types import DesiredOrder, TimeInForce
        flip = DesiredOrder(
            client_order_id="0xflip",
            symbol="BTC-PERP",
            price=50100.0,
            size=0.1,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.ALO,
        )
        om.compute_flip_order = MagicMock(return_value=flip)

        sup = _make_supervisor(order_manager=om)
        symbol = sup._config.assets[0].symbol
        sup._asset_states[symbol].grid_config = GridConfig(
            symbol=symbol, anchor=50000.0, range_atr=2.5, step_bps=20.0, epoch=1,
        )

        await sup._route_fill(_fill(is_partial=False))

        flips = sup._asset_states[symbol].pending_flips
        assert len(flips) == 1
        assert flips[0].price == 50100.0
        assert flips[0].side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_fill_for_unknown_symbol_ignored(self):
        pm = _mock_pnl_monitor()
        sup = _make_supervisor(pnl_monitor=pm)
        await sup._route_fill(_fill(symbol="UNKNOWN-PERP"))
        pm.record_fill.assert_not_called()


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


class TestAlerting:
    @pytest.mark.asyncio
    async def test_alert_callback_invoked(self):
        cb = AsyncMock()
        sup = _make_supervisor()
        sup.set_alert_callback(cb)

        await sup._send_alert("WARNING", "hello")
        cb.assert_awaited_once_with("WARNING", "hello")

    @pytest.mark.asyncio
    async def test_alert_without_callback_does_not_raise(self):
        sup = _make_supervisor()
        await sup._send_alert("INFO", "no transport configured")


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


class TestMaintenance:
    @pytest.mark.asyncio
    async def test_maintenance_marks_assets(self):
        sup = _make_supervisor()
        await sup._handle_maintenance()
        for state in sup._asset_states.values():
            assert state.bot_state == BotState.MAINTENANCE

    @pytest.mark.asyncio
    async def test_maintenance_error_does_not_count(self):
        rm = _mock_risk_manager()
        sup = _make_supervisor(risk_manager=rm)
        await sup._handle_maintenance()
        rm.record_error.assert_called_once_with(is_maintenance=True)

    def test_classifies_connection_errors_as_maintenance(self):
        assert Supervisor._looks_like_maintenance(ConnectionError("refused"))
        assert Supervisor._looks_like_maintenance(TimeoutError())
        assert Supervisor._looks_like_maintenance(Exception("HTTP 503 Service Unavailable"))
        assert Supervisor._looks_like_maintenance(Exception("connection refused"))
        assert not Supervisor._looks_like_maintenance(ValueError("bad parse"))

    def test_classifies_gateway_errors_as_maintenance(self):
        # CloudFront/nginx-fronted 502s and 504s during an exchange-side
        # outage are the same "wait passively, don't count toward kill
        # switch" class as 503/connection-refused (design.md line 109's
        # "or similar patterns") — observed live on the testnet soak where
        # a ~50min 502 Bad Gateway outage tripped max_consecutive_errors.
        assert Supervisor._looks_like_maintenance(
            ServerError(502, "<html>502 Bad Gateway</html>")
        )
        assert Supervisor._looks_like_maintenance(Exception("Bad Gateway"))
        assert Supervisor._looks_like_maintenance(
            ServerError(504, "<html>504 Gateway Timeout</html>")
        )

    @pytest.mark.asyncio
    async def test_desync_kill_routes_to_maintenance_on_gateway_reconnect_error(self):
        # Observed live 2026-08-19T19:50: RiskManager's desync check
        # (risk_manager.py's _check_errors_and_desync) fires KILL purely
        # off elapsed WS-stale time — it never goes through an exchange
        # call _looks_like_maintenance could classify, so a 502 storm that
        # takes WS down trips KILL before the (already-fixed) consecutive-
        # errors path ever gets a chance to reclassify it. The bot only
        # survived that incident because cancel_all_orders' own call
        # happened to also 502 and got caught by the outer handler — this
        # test locks in the direct fix instead of relying on that coincidence.
        rm = _mock_risk_manager(
            action=RiskAction.KILL, reason="desynced 30.5s >= 30s"
        )
        rm.evaluate = MagicMock(return_value=RiskDecision(
            action=RiskAction.KILL,
            reason="desynced 30.5s >= 30s",
            details={"desynced_seconds": 30.5},
        ))
        md = _mock_market_data()
        md.get_last_ws_message_ms = MagicMock(return_value=1)
        md.get_last_reconnect_error = MagicMock(
            return_value=ServerError(502, "<html>502 Bad Gateway</html>")
        )
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, market_data=md, order_manager=om)
        symbol = sup._config.assets[0].symbol

        await sup._run_cycle(symbol, sup._config.assets[0])

        assert sup._asset_states[symbol].bot_state == BotState.MAINTENANCE
        om.cancel_all_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_desync_kill_dispatches_normally_without_maintenance_error(self):
        # Same desync-KILL decision, but no maintenance-pattern reconnect
        # error on record (e.g. a genuine local desync bug) — must still
        # kill the bot rather than silently swallowing every desync.
        rm = _mock_risk_manager()
        rm.evaluate = MagicMock(return_value=RiskDecision(
            action=RiskAction.KILL,
            reason="desynced 30.5s >= 30s",
            details={"desynced_seconds": 30.5},
        ))
        md = _mock_market_data()
        md.get_last_ws_message_ms = MagicMock(return_value=1)
        md.get_last_reconnect_error = MagicMock(return_value=None)
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, market_data=md, order_manager=om)
        symbol = sup._config.assets[0].symbol

        await sup._run_cycle(symbol, sup._config.assets[0])

        assert sup._asset_states[symbol].bot_state == BotState.DEAD
        om.cancel_all_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_cycle_exits_maintenance_when_ws_healthy(self):
        md = _mock_market_data()
        md.is_ws_connected = MagicMock(return_value=True)
        # last WS msg is 0ms ago (fresh)
        md.get_last_ws_message_ms = MagicMock(return_value=int(9e18))
        sup = _make_supervisor(market_data=md)
        symbol = sup._config.assets[0].symbol
        sup._asset_states[symbol].bot_state = BotState.MAINTENANCE

        await sup._run_cycle(symbol, sup._config.assets[0])
        assert sup._asset_states[symbol].bot_state == BotState.RUNNING


# ---------------------------------------------------------------------------
# Desync + skew routing
# ---------------------------------------------------------------------------


class TestDesync:
    @pytest.mark.asyncio
    async def test_records_desync_when_ws_stale(self):
        rm = _mock_risk_manager()
        md = _mock_market_data()
        # Force a stale WS timestamp (>10s ago)
        md.get_last_ws_message_ms = MagicMock(return_value=1)
        sup = _make_supervisor(risk_manager=rm, market_data=md)

        asset_cfg = sup._config.assets[0]
        sup._asset_states[asset_cfg.symbol].bot_state = BotState.RUNNING
        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        rm.record_desync.assert_called()


class TestSkewReconcile:
    @pytest.mark.asyncio
    async def test_skew_inventory_still_runs_reconcile(self):
        """SKEW_INVENTORY must not skip reconcile — GridEngine applies the skew."""
        rm = _mock_risk_manager(action=RiskAction.SKEW_INVENTORY)
        om = _mock_order_manager()
        sup = _make_supervisor(risk_manager=rm, order_manager=om)

        asset_cfg = sup._config.assets[0]
        sup._asset_states[asset_cfg.symbol].bot_state = BotState.RUNNING
        await sup._run_cycle(asset_cfg.symbol, asset_cfg)
        om.reconcile.assert_awaited()  # reconcile must still run

    @pytest.mark.asyncio
    async def test_pause_grid_skips_reconcile_but_persists(self):
        rm = _mock_risk_manager(action=RiskAction.PAUSE_GRID)
        om = _mock_order_manager()
        ss = _mock_state_store()
        sup = _make_supervisor(risk_manager=rm, order_manager=om, state_store=ss)

        asset_cfg = sup._config.assets[0]
        sup._asset_states[asset_cfg.symbol].bot_state = BotState.RUNNING
        await sup._run_cycle(asset_cfg.symbol, asset_cfg)

        om.reconcile.assert_not_awaited()
        om.reconcile_with_backstop.assert_not_awaited()
        # Persistence and heartbeat still run
        ss.save_bot_state.assert_awaited()
        ss.update_heartbeat.assert_awaited()

    @pytest.mark.asyncio
    async def test_cooldown_updates_heartbeat(self):
        ss = _mock_state_store()
        sup = _make_supervisor(state_store=ss)
        symbol = sup._config.assets[0].symbol
        state = sup._asset_states[symbol]
        state.bot_state = BotState.COOLDOWN
        state.cooldown_until_ms = 2**62

        await sup._run_cycle(symbol, sup._config.assets[0])
        ss.update_heartbeat.assert_awaited()
