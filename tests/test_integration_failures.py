"""Failure-mode integration tests (Phase 8.2).

Exercises how the Supervisor responds to the hostile conditions the design
doc calls out: WS drops, 503/maintenance, drawdown kills, breakouts, and
restart orphan cleanup. Modules are wired as in test_integration.py (real
Supervisor + real StateStore against a FakeExchange), with specific failures
injected at the boundary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gridbot.config import AssetConfig, BotConfig, OperationalConfig, PortfolioConfig
from gridbot.pnl_monitor import PnLMonitor
from gridbot.risk_manager import RiskAction, RiskDecision, RiskManager
from gridbot.state_store import StateStore
from gridbot.supervisor import Supervisor
from gridbot.types import (
    AssetState,
    BotState,
    GridConfig,
    OpenOrder,
    OrderSide,
    Position,
    Regime,
    VolMetrics,
)

from tests.test_integration import (
    FakeExchange,
    _make_market_data,
    _make_order_manager,
    _build_config,
)


async def _build_sup(
    tmp_path: Path,
    exchange: FakeExchange,
    *,
    risk_decision: RiskDecision | None = None,
    db_path: Path | None = None,
) -> tuple[Supervisor, StateStore]:
    cfg = _build_config(tmp_path)
    store = StateStore(db_path or (tmp_path / "gridbot.db"))

    md = _make_market_data(exchange)
    om = _make_order_manager(exchange)

    rm = MagicMock()
    rm.evaluate = MagicMock(
        return_value=risk_decision
        or RiskDecision(action=RiskAction.CONTINUE, reason="ok")
    )
    rm.detect_regime = MagicMock(return_value=Regime.RANGE)
    rm.preflight_check = MagicMock(return_value=[])
    rm.record_equity = MagicMock()
    rm.record_vol = MagicMock()
    rm.load_vol_history = MagicMock()
    rm.record_error = MagicMock()
    rm.record_desync = MagicMock()
    rm.clear_errors = MagicMock()
    rm.clear_desync = MagicMock()

    pnl = MagicMock()
    pnl.record_fill = MagicMock()
    pnl.crosscheck = AsyncMock(return_value=False)

    sup = Supervisor(
        cfg,
        state_store=store,
        market_data=md,
        order_manager=om,
        risk_manager=rm,
        pnl_monitor=pnl,
    )
    return sup, store


# ---------------------------------------------------------------------------
# WS drop -> REST reconciliation recovers
# ---------------------------------------------------------------------------


class TestWSDropRecovery:
    @pytest.mark.asyncio
    async def test_rest_reconciliation_adopts_exchange_orders_on_ws_drift(self, tmp_path):
        """When WS-maintained state drifts from REST, the reconciler replaces
        the local open_orders list with the REST snapshot."""
        exchange = FakeExchange()
        sup, _ = await _build_sup(tmp_path, exchange)
        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"
        state = sup._asset_states[symbol]

        # Local believes there are two orders; exchange reports only one
        local_orders = [
            OpenOrder(1, "cloid-A", symbol, 49_990.0, 0.1, 0.1, OrderSide.BUY),
            OpenOrder(2, "cloid-B", symbol, 50_010.0, 0.1, 0.1, OrderSide.SELL),
        ]
        state.open_orders = list(local_orders)
        exchange.open_orders[symbol] = [local_orders[0]]  # only A survives

        await sup._rest_reconciliation(symbol)

        assert {o.client_order_id for o in state.open_orders} == {"cloid-A"}

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_rest_reconciliation_adopts_exchange_position(self, tmp_path):
        exchange = FakeExchange()
        sup, _ = await _build_sup(tmp_path, exchange)
        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"
        state = sup._asset_states[symbol]
        state.position = Position(symbol, 0.5, 49_500.0, 0.0)
        exchange.positions[symbol] = Position(symbol, 0.3, 49_600.0, 0.0)

        await sup._rest_reconciliation(symbol)

        assert state.position is not None
        assert state.position.size == 0.3

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# 503 / maintenance -> MAINTENANCE mode (no kill switch)
# ---------------------------------------------------------------------------


class TestMaintenanceMode:
    @pytest.mark.asyncio
    async def test_503_in_cycle_enters_maintenance_not_kill(self, tmp_path):
        """A 503-ish exception raised inside _run_cycle triggers MAINTENANCE
        and does not increment the error counter."""
        exchange = FakeExchange()
        sup, _ = await _build_sup(tmp_path, exchange)
        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"

        # Simulate a 503 coming out of the equity fetch
        async def _boom() -> float:
            raise Exception("HTTP 503 Service Unavailable")

        sup._market_data.fetch_account_equity = AsyncMock(side_effect=_boom)

        # Exercise the main loop guard directly: _main_loop wraps _run_cycle
        # and classifies the exception. We call _run_cycle inside that same
        # try/except via a tiny spin of the loop.
        sup._shutdown_requested = False
        # Limit to a single iteration
        orig_sleep = asyncio.sleep

        async def _shortcircuit(_t):
            sup._shutdown_requested = True
            await orig_sleep(0)

        import gridbot.supervisor as sup_mod
        original_sleep = sup_mod.asyncio.sleep
        sup_mod.asyncio.sleep = _shortcircuit  # type: ignore[attr-defined]
        try:
            await sup._main_loop()
        finally:
            sup_mod.asyncio.sleep = original_sleep  # type: ignore[attr-defined]

        assert sup._asset_states[symbol].bot_state == BotState.MAINTENANCE
        # record_error called only with is_maintenance=True
        calls = sup._risk_manager.record_error.call_args_list
        assert all(c.kwargs.get("is_maintenance") is True for c in calls), (
            f"record_error must not count maintenance errors toward kill switch: {calls}"
        )

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_maintenance_exits_on_healthy_ws(self, tmp_path):
        """When WS recovers (connected + fresh), the next cycle reconciles via
        REST and transitions back to RUNNING."""
        exchange = FakeExchange()
        sup, _ = await _build_sup(tmp_path, exchange)
        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"
        state = sup._asset_states[symbol]
        state.bot_state = BotState.MAINTENANCE

        # Pretend WS just delivered a message
        import time
        exchange.ws_connected = True
        exchange.last_ws_ms = int(time.time() * 1000)

        await sup._run_cycle(symbol, sup._config.assets[0])

        assert state.bot_state == BotState.RUNNING

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# KILL -> DEAD state
# ---------------------------------------------------------------------------


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_decision_transitions_to_dead(self, tmp_path):
        exchange = FakeExchange()
        exchange.positions["BTC-PERP"] = Position("BTC-PERP", 0.2, 49_000.0, 0.0)
        sup, _ = await _build_sup(
            tmp_path,
            exchange,
            risk_decision=RiskDecision(
                action=RiskAction.KILL,
                reason="daily drawdown breach",
                details={"type": "drawdown"},
            ),
        )

        alerts: list[tuple[str, str]] = []

        async def _alert(sev, msg):
            alerts.append((sev, msg))

        sup.set_alert_callback(_alert)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.position = exchange.positions["BTC-PERP"]

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        assert state.bot_state == BotState.DEAD
        # Critical alert fired
        assert any(sev == "CRITICAL" for sev, _ in alerts), f"alerts={alerts}"
        # Flatten was attempted and all orders cancelled
        sup._order_manager.cancel_all_orders.assert_awaited()
        sup._order_manager.execute_flatten.assert_awaited()

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_dead_state_is_sticky_across_subsequent_cycles(self, tmp_path):
        """Once DEAD, the asset is never run again even if risk would clear."""
        exchange = FakeExchange()
        sup, _ = await _build_sup(
            tmp_path,
            exchange,
            risk_decision=RiskDecision(action=RiskAction.CONTINUE, reason="ok"),
        )
        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"
        state = sup._asset_states[symbol]
        state.bot_state = BotState.DEAD

        # Even though risk says CONTINUE, _run_cycle should not have been
        # called (main loop skips DEAD assets) — but we call _run_cycle
        # directly here to confirm the cycle itself is a no-op style guard
        # isn't present; the main loop guard is what matters, so assert on
        # that.
        call_count_before = sup._order_manager.reconcile.await_count
        # Simulate a single main-loop iteration by shortcircuiting sleep
        sup._shutdown_requested = False

        async def _sleep_once(_t):
            sup._shutdown_requested = True

        import gridbot.supervisor as sup_mod
        orig = sup_mod.asyncio.sleep
        sup_mod.asyncio.sleep = _sleep_once  # type: ignore[attr-defined]
        try:
            await sup._main_loop()
        finally:
            sup_mod.asyncio.sleep = orig  # type: ignore[attr-defined]

        # No reconcile call was made because the asset is DEAD
        assert sup._order_manager.reconcile.await_count == call_count_before
        assert state.bot_state == BotState.DEAD

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# Breakout -> CANCEL_AND_FLATTEN -> COOLDOWN
# ---------------------------------------------------------------------------


class TestBreakoutCancelFlatten:
    @pytest.mark.asyncio
    async def test_breakout_cancels_and_flattens_and_enters_cooldown(self, tmp_path):
        exchange = FakeExchange()
        exchange.positions["BTC-PERP"] = Position("BTC-PERP", 0.2, 49_000.0, 0.0)
        # Seed some resting orders on the exchange
        exchange.open_orders["BTC-PERP"] = [
            OpenOrder(1, "c1", "BTC-PERP", 49_500.0, 0.1, 0.1, OrderSide.BUY),
            OpenOrder(2, "c2", "BTC-PERP", 50_500.0, 0.1, 0.1, OrderSide.SELL),
        ]

        sup, _ = await _build_sup(
            tmp_path,
            exchange,
            risk_decision=RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="distance breakout",
                details={"type": "distance"},
            ),
        )
        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.position = exchange.positions["BTC-PERP"]

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        assert state.bot_state == BotState.COOLDOWN
        assert state.cooldown_until_ms is not None
        assert state.last_breakout_ms is not None, (
            "breakout-type actions must stamp last_breakout_ms for regime cooldown"
        )
        # Orders cancelled, flatten executed
        sup._order_manager.cancel_all_orders.assert_awaited()
        sup._order_manager.execute_flatten.assert_awaited()

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_non_breakout_cancel_flatten_does_not_stamp_breakout_timer(self, tmp_path):
        """Vol-kill or drawdown-driven flatten must not piggy-back on
        the breakout regime-cooldown timer."""
        exchange = FakeExchange()
        exchange.positions["BTC-PERP"] = Position("BTC-PERP", 0.2, 49_000.0, 0.0)

        sup, _ = await _build_sup(
            tmp_path,
            exchange,
            risk_decision=RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason="volatility kill",
                details={"type": "vol_kill"},  # not in breakout detail set
            ),
        )
        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.position = exchange.positions["BTC-PERP"]

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        assert state.bot_state == BotState.COOLDOWN
        assert state.last_breakout_ms is None

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# Restart -> orphan cleanup
# ---------------------------------------------------------------------------


class TestOrphanCleanupOnRestart:
    @pytest.mark.asyncio
    async def test_unknown_exchange_orders_are_cancelled_on_recovery(self, tmp_path):
        """Orders on the exchange whose cloids aren't in the persisted state
        are treated as orphans and cancelled during recovery (CLAUDE.md:
        'don't cancel legitimate matched orders', orphans only)."""
        exchange = FakeExchange()
        # Two orders on the exchange; only one is known locally
        known = OpenOrder(1, "cloid-known", "BTC-PERP", 49_900.0, 0.1, 0.1, OrderSide.BUY)
        orphan = OpenOrder(2, "cloid-orphan", "BTC-PERP", 50_100.0, 0.1, 0.1, OrderSide.SELL)
        exchange.open_orders["BTC-PERP"] = [known, orphan]

        sup, store = await _build_sup(tmp_path, exchange)
        # Pre-populate the store with the known order so recovery considers
        # it "persisted". Easiest path: save an AssetState carrying it.
        state = AssetState(symbol="BTC-PERP")
        state.open_orders = [known]
        state.bot_state = BotState.RUNNING
        await store.initialize()
        await store.save_bot_state("BTC-PERP", state)
        # Close the pre-seed handle so the supervisor's own initialize
        # call is independent.
        await store.close()

        await sup._initialize()
        await sup._recover_state()

        # The orphan should have been targeted for cancellation
        sup._order_manager.cancel_orders.assert_awaited()
        cancelled_arg = sup._order_manager.cancel_orders.call_args_list[-1].args[1]
        cancelled_cloids = {o.client_order_id for o in cancelled_arg}
        assert "cloid-orphan" in cancelled_cloids
        assert "cloid-known" not in cancelled_cloids

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_flattening_state_resumes_flatten_on_restart(self, tmp_path):
        """If the previous run crashed mid-flatten with a non-zero position,
        recovery resumes the flatten state machine."""
        exchange = FakeExchange()
        # Simulated leftover position on the exchange
        exchange.positions["BTC-PERP"] = Position("BTC-PERP", 0.15, 49_000.0, 0.0)

        sup, store = await _build_sup(tmp_path, exchange)
        persisted = AssetState(symbol="BTC-PERP")
        persisted.bot_state = BotState.FLATTENING
        persisted.position = exchange.positions["BTC-PERP"]
        await store.initialize()
        await store.save_bot_state("BTC-PERP", persisted)
        await store.close()

        await sup._initialize()
        await sup._recover_state()

        # execute_flatten should have been awaited during recovery
        sup._order_manager.execute_flatten.assert_awaited()

        sup._fill_task.cancel()
        await sup._shutdown()
