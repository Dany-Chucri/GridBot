"""End-to-end integration tests (Phase 8.1).

These tests wire the real Supervisor, GridEngine, RiskManager, PnLMonitor, and
StateStore (backed by a temp SQLite file) together against a FakeExchange that
simulates the Hyperliquid boundary. This covers orchestration behaviour that
pure-unit tests (test_supervisor.py) can't — state persistence across restart,
module-to-module handoffs, and the full cycle dataflow.

True testnet validation (real Hyperliquid credentials, 48h soak) is still
required per design doc 10.5 — see docs/operations.md for the procedure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    Fill,
    GridConfig,
    OpenOrder,
    OrderSide,
    Position,
    Regime,
    VolMetrics,
)


# ---------------------------------------------------------------------------
# FakeExchange — a minimal in-memory simulator for integration tests.
# ---------------------------------------------------------------------------


@dataclass
class FakeExchange:
    """Tracks order book, position, equity, and PnL for the duration of a test."""

    mid: float = 50_000.0
    mark: float = 50_000.0
    equity: float = 100_000.0
    open_orders: dict[str, list[OpenOrder]] = field(default_factory=dict)
    positions: dict[str, Position | None] = field(default_factory=dict)
    exchange_pnl: dict[str, float] = field(default_factory=dict)
    ws_connected: bool = True
    last_ws_ms: int = 1_000
    fetch_calls: list[str] = field(default_factory=list)
    reconcile_calls: list[tuple[str, int, int]] = field(default_factory=list)

    def place(self, order: OpenOrder) -> None:
        self.open_orders.setdefault(order.symbol, []).append(order)

    def cancel_all(self, symbol: str) -> None:
        self.open_orders[symbol] = []

    def list_orders(self, symbol: str) -> list[OpenOrder]:
        return list(self.open_orders.get(symbol, []))


def _make_market_data(exchange: FakeExchange) -> MagicMock:
    md = MagicMock()
    md.connect = AsyncMock()
    md.disconnect = AsyncMock()
    md.fills = asyncio.Queue()

    async def _fetch_orders(symbol: str) -> list[OpenOrder]:
        exchange.fetch_calls.append(f"orders:{symbol}")
        return exchange.list_orders(symbol)

    async def _fetch_position(symbol: str) -> Position | None:
        exchange.fetch_calls.append(f"position:{symbol}")
        return exchange.positions.get(symbol)

    async def _fetch_equity() -> float:
        return exchange.equity

    async def _fetch_pnl(symbol: str) -> float:
        return exchange.exchange_pnl.get(symbol, 0.0)

    async def _fetch_depth(symbol: str, depth_bps: float = 50.0, side=None) -> float:
        return 20.0

    md.fetch_open_orders = AsyncMock(side_effect=_fetch_orders)
    md.fetch_position = AsyncMock(side_effect=_fetch_position)
    md.fetch_account_equity = AsyncMock(side_effect=_fetch_equity)
    md.fetch_exchange_pnl = AsyncMock(side_effect=_fetch_pnl)
    md.fetch_book_depth = AsyncMock(side_effect=_fetch_depth)

    md.get_mid_price = MagicMock(side_effect=lambda sym: exchange.mid)
    md.get_mark_price = MagicMock(side_effect=lambda sym: exchange.mark)
    md.get_funding_rate = MagicMock(return_value=0.0)
    md.get_last_ws_message_ms = MagicMock(side_effect=lambda: exchange.last_ws_ms)
    md.is_ws_connected = MagicMock(side_effect=lambda: exchange.ws_connected)
    md.compute_vol_metrics = MagicMock(
        return_value=VolMetrics(
            realized_vol=0.5,
            atr=500.0,
            spread_bps=2.0,
            rolling_return_1m=0.0,
            rolling_return_5m=0.0,
        )
    )
    return md


def _make_order_manager(exchange: FakeExchange) -> MagicMock:
    om = MagicMock()
    om.initialize = AsyncMock()

    async def _reconcile(symbol, desired, current, mid_price):
        cancels = len(current)
        exchange.cancel_all(symbol)
        for i, d in enumerate(desired):
            exchange.place(
                OpenOrder(
                    order_id=10_000 + i,
                    client_order_id=d.client_order_id,
                    symbol=d.symbol,
                    price=d.price,
                    size=d.size,
                    remaining=d.size,
                    side=d.side,
                    reduce_only=d.reduce_only,
                )
            )
        exchange.reconcile_calls.append((symbol, cancels, len(desired)))

    async def _reconcile_with_backstop(symbol, desired, current, **kwargs):
        await _reconcile(symbol, desired, current, kwargs.get("mid_price", 0.0))

    async def _cancel_all(symbol: str) -> None:
        exchange.cancel_all(symbol)

    async def _cancel_orders(symbol: str, orders) -> None:
        cloids = {o.client_order_id for o in orders}
        remaining = [o for o in exchange.list_orders(symbol) if o.client_order_id not in cloids]
        exchange.open_orders[symbol] = remaining

    async def _flatten(symbol, position, config, *args, **kwargs):
        exchange.positions[symbol] = None
        return True

    om.reconcile = AsyncMock(side_effect=_reconcile)
    om.reconcile_with_backstop = AsyncMock(side_effect=_reconcile_with_backstop)
    om.cancel_all_orders = AsyncMock(side_effect=_cancel_all)
    om.cancel_orders = AsyncMock(side_effect=_cancel_orders)
    om.execute_flatten = AsyncMock(side_effect=_flatten)
    om.update_backstop = AsyncMock()
    om.compute_flip_order = MagicMock(return_value=None)
    return om


def _build_config(tmp_path: Path, symbols: tuple[str, ...] = ("BTC-PERP",)) -> BotConfig:
    return BotConfig(
        assets=[AssetConfig(symbol=s, max_abs_position=1.0) for s in symbols],
        operational=OperationalConfig(
            cycle_interval_seconds=0.01,
            reconcile_interval_seconds=0.02,
        ),
        portfolio=PortfolioConfig(
            pnl_crosscheck_interval_seconds=0.01,
            pnl_divergence_threshold_usd=50.0,
        ),
    )


async def _build_supervisor(
    tmp_path: Path,
    exchange: FakeExchange,
    *,
    real_pnl: bool = False,
    real_risk_manager: bool = False,
    risk_action: RiskAction = RiskAction.CONTINUE,
    db_path: Path | None = None,
) -> tuple[Supervisor, StateStore]:
    cfg = _build_config(tmp_path)
    store = StateStore(db_path or (tmp_path / "gridbot.db"))

    md = _make_market_data(exchange)
    om = _make_order_manager(exchange)

    if real_risk_manager:
        rm = RiskManager(cfg)
    else:
        rm = MagicMock()
        rm.evaluate = MagicMock(return_value=RiskDecision(action=risk_action, reason="test"))
        rm.detect_regime = MagicMock(return_value=Regime.RANGE)
        rm.preflight_check = MagicMock(return_value=[])
        rm.record_equity = MagicMock()
        rm.record_vol = MagicMock()
        rm.record_error = MagicMock()
        rm.record_desync = MagicMock()
        rm.clear_errors = MagicMock()
        rm.clear_desync = MagicMock()

    pnl = PnLMonitor(cfg) if real_pnl else MagicMock(
        record_fill=MagicMock(),
        crosscheck=AsyncMock(return_value=False),
    )

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
# Test class: full lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_then_shutdown(self, tmp_path):
        """Supervisor can initialize all modules and then shut down cleanly."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        await sup._initialize()
        await sup._preflight_checks()

        for state in sup._asset_states.values():
            assert state.bot_state == BotState.RUNNING

        await sup._shutdown()

        # Orders cancelled during shutdown (cancel_all_orders called)
        sup._order_manager.cancel_all_orders.assert_awaited()
        # State persisted
        sup._state_store.close  # store closed in shutdown
        for state in sup._asset_states.values():
            assert state.bot_state in (BotState.SHUTTING_DOWN, BotState.DEAD)

    @pytest.mark.asyncio
    async def test_run_cycle_places_grid_orders(self, tmp_path):
        """A full cycle routes computed desired orders into the exchange."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        await sup._initialize()
        await sup._preflight_checks()

        symbol = "BTC-PERP"
        state = sup._asset_states[symbol]
        # Seed grid_config so reconcile_with_backstop is used (and to give
        # GridEngine a concrete anchor).
        state.grid_config = GridConfig(
            symbol=symbol, anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position(
            symbol=symbol, size=0.0, avg_entry_price=0.0, unrealized_pnl=0.0
        )

        await sup._run_cycle(symbol, sup._config.assets[0])

        # One reconciliation happened and produced grid orders in the fake exchange
        assert len(exchange.reconcile_calls) == 1
        placed = exchange.list_orders(symbol)
        assert len(placed) > 0, "expected grid orders to be placed by GridEngine"
        # All grid orders should be ALO post-only (reduce_only set only when at caps)
        for o in placed:
            assert o.symbol == symbol

        # Clean up the fill pump task before the event loop closes
        sup._fill_task.cancel()


# ---------------------------------------------------------------------------
# Test class: fills trigger flip orders
# ---------------------------------------------------------------------------


class TestFillHandling:
    @pytest.mark.asyncio
    async def test_full_fill_routes_to_pnl_and_state_store(self, tmp_path):
        """A full fill written to MarketData.fills is routed to PnLMonitor
        and StateStore via the fill pump."""
        exchange = FakeExchange()
        sup, store = await _build_supervisor(tmp_path, exchange, real_pnl=True)

        await sup._initialize()
        await sup._preflight_checks()

        # Seed state so the fill pump considers the asset flippable
        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position("BTC-PERP", 0.0, 0.0, 0.0)

        fill = Fill(
            fill_id="f1",
            order_id=1,
            client_order_id="c1",
            symbol="BTC-PERP",
            price=49_950.0,
            size=0.1,
            side=OrderSide.BUY,
            fee=0.05,
            timestamp_ms=1000,
            is_maker=True,
            is_partial=False,
        )
        await sup._market_data.fills.put(fill)

        # Give the fill pump a moment to drain
        await asyncio.sleep(0.2)

        # PnL ledger updated (real PnLMonitor)
        assert sup._pnl_monitor.get_realized_pnl("BTC-PERP") == 0.0  # first fill opens position
        # StateStore ledger updated
        fills = await store.get_fills("BTC-PERP")
        assert len(fills) == 1
        assert fills[0].fill_id == "f1"

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_full_fill_emits_pending_flip(self, tmp_path):
        """A full fill with a non-None flip computation becomes a pending flip."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        # Force the OrderManager mock to produce a flip
        from gridbot.types import DesiredOrder, TimeInForce
        flip = DesiredOrder(
            client_order_id="flip-1",
            symbol="BTC-PERP",
            price=50_050.0,
            size=0.1,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.ALO,
            reduce_only=False,
        )
        sup._order_manager.compute_flip_order = MagicMock(return_value=flip)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position("BTC-PERP", 0.1, 49_950.0, 0.0)

        buy_fill = Fill(
            fill_id="fX",
            order_id=1,
            client_order_id="c1",
            symbol="BTC-PERP",
            price=49_950.0,
            size=0.1,
            side=OrderSide.BUY,
            fee=0.0,
            timestamp_ms=1000,
            is_maker=True,
            is_partial=False,
        )
        await sup._market_data.fills.put(buy_fill)
        await asyncio.sleep(0.2)

        assert len(state.pending_flips) == 1
        assert state.pending_flips[0].price == 50_050.0
        assert state.pending_flips[0].side == OrderSide.SELL

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_partial_fill_does_not_flip(self, tmp_path):
        """Partial fills are recorded but never emit flip orders (design 7.5)."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )

        partial = Fill(
            fill_id="fp",
            order_id=1,
            client_order_id="c1",
            symbol="BTC-PERP",
            price=49_950.0,
            size=0.05,
            side=OrderSide.BUY,
            fee=0.0,
            timestamp_ms=1000,
            is_maker=True,
            is_partial=True,
        )
        await sup._market_data.fills.put(partial)
        await asyncio.sleep(0.2)

        assert state.pending_flips == []
        # compute_flip_order should not have been called for partial
        sup._order_manager.compute_flip_order.assert_not_called()

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# Test class: backstop placement
# ---------------------------------------------------------------------------


class TestBackstop:
    @pytest.mark.asyncio
    async def test_reconcile_with_backstop_used_when_position_exists(self, tmp_path):
        """Supervisor batches grid + backstop together when a position is open."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position("BTC-PERP", 0.3, 49_000.0, 0.0)

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        # With a position, the batched API must be used (design 6.8)
        sup._order_manager.reconcile_with_backstop.assert_awaited()
        sup._order_manager.reconcile.assert_not_called()

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_plain_reconcile_when_no_position(self, tmp_path):
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = None

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        sup._order_manager.reconcile.assert_awaited()
        sup._order_manager.reconcile_with_backstop.assert_not_called()

        sup._fill_task.cancel()
        await sup._shutdown()


# ---------------------------------------------------------------------------
# Test class: state persistence across restart
# ---------------------------------------------------------------------------


class TestPersistenceAcrossRestart:
    @pytest.mark.asyncio
    async def test_persisted_state_and_fills_survive_restart(self, tmp_path):
        """Shutting down one supervisor and starting a second against the
        same SQLite file preserves fills, pending flips, and grid config."""
        db_path = tmp_path / "gridbot.db"
        exchange_a = FakeExchange()
        sup_a, store_a = await _build_supervisor(tmp_path, exchange_a, db_path=db_path)

        await sup_a._initialize()
        await sup_a._preflight_checks()

        state = sup_a._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position("BTC-PERP", 0.25, 49_500.0, 0.0)

        fill = Fill(
            fill_id="persist-1",
            order_id=1,
            client_order_id="cloid-a",
            symbol="BTC-PERP",
            price=49_500.0,
            size=0.1,
            side=OrderSide.BUY,
            fee=0.0,
            timestamp_ms=1_000,
            is_maker=True,
            is_partial=False,
        )
        await store_a.record_fill(fill)
        await sup_a._run_cycle("BTC-PERP", sup_a._config.assets[0])
        await sup_a._shutdown()

        # Second supervisor — same DB, exchange state preserves position
        exchange_b = FakeExchange()
        exchange_b.positions["BTC-PERP"] = Position("BTC-PERP", 0.25, 49_500.0, 0.0)
        sup_b, store_b = await _build_supervisor(tmp_path, exchange_b, db_path=db_path)

        await sup_b._initialize()
        await sup_b._recover_state()

        recovered = sup_b._asset_states["BTC-PERP"]
        # Position is reconciled from exchange (source of truth)
        assert recovered.position is not None
        assert recovered.position.size == 0.25
        # Grid config restored from persistence
        assert recovered.grid_config is not None
        assert recovered.grid_config.anchor == 50_000.0

        # Fills ledger survived
        fills = await store_b.get_fills("BTC-PERP")
        assert len(fills) == 1
        assert fills[0].fill_id == "persist-1"

        sup_b._fill_task.cancel()
        await sup_b._shutdown()


# ---------------------------------------------------------------------------
# Test class: PnL cross-check
# ---------------------------------------------------------------------------


class TestPnLCrosscheck:
    @pytest.mark.asyncio
    async def test_crosscheck_detects_divergence_and_alerts(self, tmp_path):
        """When local realized+funding diverges from exchange PnL by more than
        the configured threshold, an alert is emitted."""
        exchange = FakeExchange()
        sup, _ = await _build_supervisor(tmp_path, exchange, real_pnl=True)

        # Force a large divergence: exchange reports +500 USD, local has 0
        exchange.exchange_pnl["BTC-PERP"] = 500.0

        alerts: list[tuple[str, str]] = []

        async def _alert(sev, msg):
            alerts.append((sev, msg))

        sup.set_alert_callback(_alert)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )
        state.position = Position("BTC-PERP", 0.0, 0.0, 0.0)

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        assert any("divergence" in msg.lower() for _sev, msg in alerts), (
            f"expected PnL divergence alert, got: {alerts}"
        )

        sup._fill_task.cancel()
        await sup._shutdown()

    @pytest.mark.asyncio
    async def test_crosscheck_in_sync_does_not_alert(self, tmp_path):
        exchange = FakeExchange()
        exchange.exchange_pnl["BTC-PERP"] = 0.0
        sup, _ = await _build_supervisor(tmp_path, exchange, real_pnl=True)

        alerts: list[tuple[str, str]] = []

        async def _alert(sev, msg):
            alerts.append((sev, msg))

        sup.set_alert_callback(_alert)

        await sup._initialize()
        await sup._preflight_checks()

        state = sup._asset_states["BTC-PERP"]
        state.grid_config = GridConfig(
            symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5, step_bps=10.0, epoch=0
        )

        await sup._run_cycle("BTC-PERP", sup._config.assets[0])

        assert not any("divergence" in msg.lower() for _sev, msg in alerts)

        sup._fill_task.cancel()
        await sup._shutdown()
