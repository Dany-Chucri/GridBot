"""Tests for StateStore — SQLite persistence layer."""

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from gridbot.state_store import StateStore
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
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path: Path):
    """Create a fresh StateStore backed by a temp file."""
    db_path = tmp_path / "test.db"
    s = StateStore(db_path=db_path)
    await s.initialize()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grid_config(symbol: str = "BTC-PERP", anchor: float = 60000.0) -> GridConfig:
    return GridConfig(
        symbol=symbol, anchor=anchor, range_atr=2.5, step_bps=12.0, epoch=1
    )


def _position(symbol: str = "BTC-PERP", size: float = 0.5) -> Position:
    return Position(
        symbol=symbol,
        size=size,
        avg_entry_price=59500.0,
        unrealized_pnl=250.0,
        liquidation_price=55000.0,
    )


def _open_order(
    client_order_id: str = "grid_btc_1",
    symbol: str = "BTC-PERP",
    price: float = 59000.0,
    side: OrderSide = OrderSide.BUY,
) -> OpenOrder:
    return OpenOrder(
        order_id=1001,
        client_order_id=client_order_id,
        symbol=symbol,
        price=price,
        size=0.01,
        remaining=0.01,
        side=side,
        reduce_only=False,
    )


def _fill(
    fill_id: str = "f1",
    symbol: str = "BTC-PERP",
    timestamp_ms: int = 1000,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=1001,
        client_order_id="grid_btc_1",
        symbol=symbol,
        price=59000.0,
        size=0.01,
        side=OrderSide.BUY,
        fee=0.0012,
        timestamp_ms=timestamp_ms,
        is_maker=True,
    )


def _pending_flip(
    price: float = 59500.0,
    originating_fill_id: str = "f1",
) -> PendingFlip:
    return PendingFlip(
        price=price,
        side=OrderSide.SELL,
        size=0.01,
        originating_fill_id=originating_fill_id,
    )


def _asset_state(symbol: str = "BTC-PERP") -> AssetState:
    return AssetState(
        symbol=symbol,
        mid_price=60000.0,
        mark_price=60010.0,
        regime=Regime.RANGE,
        bot_state=BotState.RUNNING,
        position=_position(symbol),
        vol_metrics=VolMetrics(
            realized_vol=0.5, atr=500.0, spread_bps=1.0,
            rolling_return_1m=0.001, rolling_return_5m=0.003, baseline_vol=0.45,
        ),
        grid_config=_grid_config(symbol),
        open_orders=[_open_order(symbol=symbol)],
        pending_flips=[_pending_flip()],
        cooldown_until_ms=None,
        account_equity=100000.0,
        funding_rate=0.01,
        moving_avg=59800.0,
        last_breakout_ms=None,
        stagger_placed_count=3,
        drift_start_ms=None,
        anchor_epoch=1,
    )


# ---------------------------------------------------------------------------
# 3.1 Initialize — tables and WAL
# ---------------------------------------------------------------------------

class TestInitialize:
    @pytest.mark.asyncio
    async def test_creates_tables(self, store: StateStore):
        """initialize() creates all required tables."""
        cursor = await store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        rows = await cursor.fetchall()
        table_names = {row["name"] for row in rows}
        expected = {
            "schema_version", "grid_config", "positions", "open_orders",
            "fills", "regime", "pending_flips", "bot_state", "heartbeat",
        }
        assert expected.issubset(table_names)

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, store: StateStore):
        """WAL journal mode is active for crash safety."""
        cursor = await store._conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0] == "wal"

    @pytest.mark.asyncio
    async def test_schema_version_recorded(self, store: StateStore):
        cursor = await store._conn.execute("SELECT MAX(version) as v FROM schema_version")
        row = await cursor.fetchone()
        assert row["v"] == 1

    @pytest.mark.asyncio
    async def test_creates_data_directory(self, tmp_path: Path):
        """initialize() creates parent directory if missing."""
        db_path = tmp_path / "subdir" / "nested" / "test.db"
        s = StateStore(db_path=db_path)
        await s.initialize()
        assert db_path.parent.exists()
        await s.close()

    @pytest.mark.asyncio
    async def test_idempotent_initialize(self, tmp_path: Path):
        """Calling initialize() twice on the same DB doesn't corrupt it."""
        db_path = tmp_path / "test.db"
        s1 = StateStore(db_path=db_path)
        await s1.initialize()
        await s1.save_grid_config(_grid_config())
        await s1.close()

        s2 = StateStore(db_path=db_path)
        await s2.initialize()
        loaded = await s2.load_grid_config("BTC-PERP")
        assert loaded is not None
        assert loaded.anchor == 60000.0
        await s2.close()


# ---------------------------------------------------------------------------
# 3.2 Grid config
# ---------------------------------------------------------------------------

class TestGridConfig:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        config = _grid_config()
        await store.save_grid_config(config)
        loaded = await store.load_grid_config("BTC-PERP")
        assert loaded == config

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, store: StateStore):
        assert await store.load_grid_config("ETH-PERP") is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, store: StateStore):
        await store.save_grid_config(_grid_config(anchor=60000.0))
        await store.save_grid_config(_grid_config(anchor=61000.0))
        loaded = await store.load_grid_config("BTC-PERP")
        assert loaded.anchor == 61000.0

    @pytest.mark.asyncio
    async def test_multiple_symbols(self, store: StateStore):
        btc = _grid_config("BTC-PERP", 60000.0)
        eth = _grid_config("ETH-PERP", 3000.0)
        await store.save_grid_config(btc)
        await store.save_grid_config(eth)
        assert (await store.load_grid_config("BTC-PERP")).anchor == 60000.0
        assert (await store.load_grid_config("ETH-PERP")).anchor == 3000.0


# ---------------------------------------------------------------------------
# 3.2 Position
# ---------------------------------------------------------------------------

class TestPosition:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        pos = _position()
        await store.save_position(pos)
        loaded = await store.load_position("BTC-PERP")
        assert loaded.size == pos.size
        assert loaded.avg_entry_price == pos.avg_entry_price
        assert loaded.unrealized_pnl == pos.unrealized_pnl
        assert loaded.liquidation_price == pos.liquidation_price

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, store: StateStore):
        assert await store.load_position("ETH-PERP") is None

    @pytest.mark.asyncio
    async def test_null_liquidation_price(self, store: StateStore):
        pos = Position(symbol="ETH-PERP", size=1.0, avg_entry_price=3000.0,
                       unrealized_pnl=0.0, liquidation_price=None)
        await store.save_position(pos)
        loaded = await store.load_position("ETH-PERP")
        assert loaded.liquidation_price is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, store: StateStore):
        await store.save_position(_position(size=0.5))
        await store.save_position(_position(size=-0.3))
        loaded = await store.load_position("BTC-PERP")
        assert loaded.size == -0.3


# ---------------------------------------------------------------------------
# 3.2 Open orders — bulk replace
# ---------------------------------------------------------------------------

class TestOpenOrders:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        orders = [
            _open_order("o1", price=59000.0, side=OrderSide.BUY),
            _open_order("o2", price=61000.0, side=OrderSide.SELL),
        ]
        await store.save_open_orders("BTC-PERP", orders)
        loaded = await store.load_open_orders("BTC-PERP")
        assert len(loaded) == 2
        ids = {o.client_order_id for o in loaded}
        assert ids == {"o1", "o2"}

    @pytest.mark.asyncio
    async def test_bulk_replace(self, store: StateStore):
        """Second save replaces all orders for that symbol."""
        batch1 = [_open_order("o1"), _open_order("o2")]
        await store.save_open_orders("BTC-PERP", batch1)

        batch2 = [_open_order("o3")]
        await store.save_open_orders("BTC-PERP", batch2)

        loaded = await store.load_open_orders("BTC-PERP")
        assert len(loaded) == 1
        assert loaded[0].client_order_id == "o3"

    @pytest.mark.asyncio
    async def test_empty_save_clears(self, store: StateStore):
        await store.save_open_orders("BTC-PERP", [_open_order("o1")])
        await store.save_open_orders("BTC-PERP", [])
        loaded = await store.load_open_orders("BTC-PERP")
        assert loaded == []

    @pytest.mark.asyncio
    async def test_load_missing_returns_empty(self, store: StateStore):
        assert await store.load_open_orders("ETH-PERP") == []

    @pytest.mark.asyncio
    async def test_side_enum_round_trip(self, store: StateStore):
        orders = [_open_order("o1", side=OrderSide.SELL)]
        await store.save_open_orders("BTC-PERP", orders)
        loaded = await store.load_open_orders("BTC-PERP")
        assert loaded[0].side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_reduce_only_round_trip(self, store: StateStore):
        order = _open_order("o1")
        order.reduce_only = True
        await store.save_open_orders("BTC-PERP", [order])
        loaded = await store.load_open_orders("BTC-PERP")
        assert loaded[0].reduce_only is True

    @pytest.mark.asyncio
    async def test_symbol_isolation(self, store: StateStore):
        """Orders for different symbols don't interfere."""
        await store.save_open_orders("BTC-PERP", [_open_order("btc1", symbol="BTC-PERP")])
        await store.save_open_orders("ETH-PERP", [_open_order("eth1", symbol="ETH-PERP")])
        btc = await store.load_open_orders("BTC-PERP")
        eth = await store.load_open_orders("ETH-PERP")
        assert len(btc) == 1 and btc[0].client_order_id == "btc1"
        assert len(eth) == 1 and eth[0].client_order_id == "eth1"


# ---------------------------------------------------------------------------
# 3.2 Fills — append-only
# ---------------------------------------------------------------------------

class TestFills:
    @pytest.mark.asyncio
    async def test_record_and_get(self, store: StateStore):
        fill = _fill("f1", timestamp_ms=1000)
        await store.record_fill(fill)
        fills = await store.get_fills("BTC-PERP")
        assert len(fills) == 1
        assert fills[0].fill_id == "f1"
        assert fills[0].is_maker is True

    @pytest.mark.asyncio
    async def test_multiple_fills(self, store: StateStore):
        for i in range(5):
            await store.record_fill(_fill(f"f{i}", timestamp_ms=1000 + i))
        fills = await store.get_fills("BTC-PERP")
        assert len(fills) == 5

    @pytest.mark.asyncio
    async def test_since_ms_filter(self, store: StateStore):
        await store.record_fill(_fill("f1", timestamp_ms=1000))
        await store.record_fill(_fill("f2", timestamp_ms=2000))
        await store.record_fill(_fill("f3", timestamp_ms=3000))

        fills = await store.get_fills("BTC-PERP", since_ms=2000)
        assert len(fills) == 2
        assert fills[0].fill_id == "f2"
        assert fills[1].fill_id == "f3"

    @pytest.mark.asyncio
    async def test_since_ms_returns_empty(self, store: StateStore):
        await store.record_fill(_fill("f1", timestamp_ms=1000))
        fills = await store.get_fills("BTC-PERP", since_ms=5000)
        assert fills == []

    @pytest.mark.asyncio
    async def test_ordered_by_timestamp(self, store: StateStore):
        await store.record_fill(_fill("f3", timestamp_ms=3000))
        await store.record_fill(_fill("f1", timestamp_ms=1000))
        await store.record_fill(_fill("f2", timestamp_ms=2000))
        fills = await store.get_fills("BTC-PERP")
        timestamps = [f.timestamp_ms for f in fills]
        assert timestamps == [1000, 2000, 3000]

    @pytest.mark.asyncio
    async def test_symbol_isolation(self, store: StateStore):
        await store.record_fill(_fill("f1", symbol="BTC-PERP"))
        await store.record_fill(_fill("f2", symbol="ETH-PERP"))
        btc_fills = await store.get_fills("BTC-PERP")
        eth_fills = await store.get_fills("ETH-PERP")
        assert len(btc_fills) == 1
        assert len(eth_fills) == 1

    @pytest.mark.asyncio
    async def test_side_enum_round_trip(self, store: StateStore):
        fill = _fill("f1")
        await store.record_fill(fill)
        loaded = await store.get_fills("BTC-PERP")
        assert loaded[0].side == OrderSide.BUY


# ---------------------------------------------------------------------------
# 3.2 Regime
# ---------------------------------------------------------------------------

class TestRegime:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        await store.save_regime("BTC-PERP", Regime.RANGE, 1000)
        result = await store.load_regime("BTC-PERP")
        assert result == (Regime.RANGE, 1000)

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, store: StateStore):
        assert await store.load_regime("ETH-PERP") is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, store: StateStore):
        await store.save_regime("BTC-PERP", Regime.RANGE, 1000)
        await store.save_regime("BTC-PERP", Regime.TREND, 2000)
        result = await store.load_regime("BTC-PERP")
        assert result == (Regime.TREND, 2000)

    @pytest.mark.asyncio
    async def test_all_regime_values(self, store: StateStore):
        for regime in Regime:
            await store.save_regime("BTC-PERP", regime, 1000)
            result = await store.load_regime("BTC-PERP")
            assert result[0] == regime


# ---------------------------------------------------------------------------
# 3.2 Pending flips — bulk replace
# ---------------------------------------------------------------------------

class TestPendingFlips:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        flips = [_pending_flip(59500.0, "f1"), _pending_flip(60500.0, "f2")]
        await store.save_pending_flips("BTC-PERP", flips)
        loaded = await store.load_pending_flips("BTC-PERP")
        assert len(loaded) == 2
        fill_ids = {f.originating_fill_id for f in loaded}
        assert fill_ids == {"f1", "f2"}

    @pytest.mark.asyncio
    async def test_bulk_replace(self, store: StateStore):
        batch1 = [_pending_flip(59500.0, "f1")]
        await store.save_pending_flips("BTC-PERP", batch1)

        batch2 = [_pending_flip(60500.0, "f2"), _pending_flip(61000.0, "f3")]
        await store.save_pending_flips("BTC-PERP", batch2)

        loaded = await store.load_pending_flips("BTC-PERP")
        assert len(loaded) == 2
        fill_ids = {f.originating_fill_id for f in loaded}
        assert fill_ids == {"f2", "f3"}

    @pytest.mark.asyncio
    async def test_empty_save_clears(self, store: StateStore):
        await store.save_pending_flips("BTC-PERP", [_pending_flip()])
        await store.save_pending_flips("BTC-PERP", [])
        loaded = await store.load_pending_flips("BTC-PERP")
        assert loaded == []

    @pytest.mark.asyncio
    async def test_load_missing_returns_empty(self, store: StateStore):
        assert await store.load_pending_flips("ETH-PERP") == []

    @pytest.mark.asyncio
    async def test_symbol_isolation(self, store: StateStore):
        await store.save_pending_flips("BTC-PERP", [_pending_flip(59500.0, "f1")])
        await store.save_pending_flips("ETH-PERP", [_pending_flip(3050.0, "f2")])
        btc = await store.load_pending_flips("BTC-PERP")
        eth = await store.load_pending_flips("ETH-PERP")
        assert len(btc) == 1 and btc[0].originating_fill_id == "f1"
        assert len(eth) == 1 and eth[0].originating_fill_id == "f2"


# ---------------------------------------------------------------------------
# 3.2 Bot state (AssetState serialization)
# ---------------------------------------------------------------------------

class TestBotState:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")

        assert loaded.symbol == state.symbol
        assert loaded.mid_price == state.mid_price
        assert loaded.mark_price == state.mark_price
        assert loaded.regime == Regime.RANGE
        assert loaded.bot_state == BotState.RUNNING
        assert loaded.account_equity == state.account_equity
        assert loaded.funding_rate == state.funding_rate
        assert loaded.stagger_placed_count == state.stagger_placed_count
        assert loaded.anchor_epoch == state.anchor_epoch

    @pytest.mark.asyncio
    async def test_nested_position_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")
        assert loaded.position is not None
        assert loaded.position.size == state.position.size
        assert loaded.position.avg_entry_price == state.position.avg_entry_price

    @pytest.mark.asyncio
    async def test_nested_vol_metrics_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")
        assert loaded.vol_metrics is not None
        assert loaded.vol_metrics.realized_vol == state.vol_metrics.realized_vol

    @pytest.mark.asyncio
    async def test_nested_grid_config_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")
        assert loaded.grid_config is not None
        assert loaded.grid_config.anchor == state.grid_config.anchor

    @pytest.mark.asyncio
    async def test_nested_open_orders_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")
        assert len(loaded.open_orders) == 1
        assert loaded.open_orders[0].side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_nested_pending_flips_round_trip(self, store: StateStore):
        state = _asset_state()
        await store.save_bot_state("BTC-PERP", state)
        loaded = await store.load_bot_state("BTC-PERP")
        assert len(loaded.pending_flips) == 1
        assert loaded.pending_flips[0].side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_none_optionals(self, store: StateStore):
        state = AssetState(symbol="ETH-PERP")
        await store.save_bot_state("ETH-PERP", state)
        loaded = await store.load_bot_state("ETH-PERP")
        assert loaded.position is None
        assert loaded.vol_metrics is None
        assert loaded.grid_config is None
        assert loaded.cooldown_until_ms is None
        assert loaded.last_breakout_ms is None

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, store: StateStore):
        assert await store.load_bot_state("ETH-PERP") is None

    @pytest.mark.asyncio
    async def test_all_regimes_serialize(self, store: StateStore):
        for regime in Regime:
            state = AssetState(symbol="BTC-PERP", regime=regime)
            await store.save_bot_state("BTC-PERP", state)
            loaded = await store.load_bot_state("BTC-PERP")
            assert loaded.regime == regime

    @pytest.mark.asyncio
    async def test_all_bot_states_serialize(self, store: StateStore):
        for bs in BotState:
            state = AssetState(symbol="BTC-PERP", bot_state=bs)
            await store.save_bot_state("BTC-PERP", state)
            loaded = await store.load_bot_state("BTC-PERP")
            assert loaded.bot_state == bs


# ---------------------------------------------------------------------------
# 3.2 Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_round_trip(self, store: StateStore):
        await store.update_heartbeat("BTC-PERP", 12345)
        assert await store.get_last_heartbeat("BTC-PERP") == 12345

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(self, store: StateStore):
        assert await store.get_last_heartbeat("ETH-PERP") is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, store: StateStore):
        await store.update_heartbeat("BTC-PERP", 1000)
        await store.update_heartbeat("BTC-PERP", 2000)
        assert await store.get_last_heartbeat("BTC-PERP") == 2000


# ---------------------------------------------------------------------------
# 3.3 Close
# ---------------------------------------------------------------------------

class TestClose:
    @pytest.mark.asyncio
    async def test_close_commits_and_disconnects(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        s = StateStore(db_path=db_path)
        await s.initialize()
        await s.save_grid_config(_grid_config())
        await s.close()

        # Re-open and verify data persisted
        s2 = StateStore(db_path=db_path)
        await s2.initialize()
        loaded = await s2.load_grid_config("BTC-PERP")
        assert loaded is not None
        assert loaded.anchor == 60000.0
        await s2.close()

    @pytest.mark.asyncio
    async def test_close_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        s = StateStore(db_path=db_path)
        await s.initialize()
        await s.close()
        await s.close()  # Should not raise


# ---------------------------------------------------------------------------
# Cross-cutting: WAL concurrency
# ---------------------------------------------------------------------------

class TestWALConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writes_no_corruption(self, tmp_path: Path):
        """Multiple rapid writes via WAL mode don't corrupt data."""
        db_path = tmp_path / "test.db"
        s = StateStore(db_path=db_path)
        await s.initialize()

        # Rapid sequential writes to different tables
        for i in range(20):
            await s.save_grid_config(_grid_config(anchor=60000.0 + i))
            await s.update_heartbeat("BTC-PERP", 1000 + i)
            await s.record_fill(_fill(f"f{i}", timestamp_ms=1000 + i))

        # Verify integrity
        config = await s.load_grid_config("BTC-PERP")
        assert config.anchor == 60019.0

        hb = await s.get_last_heartbeat("BTC-PERP")
        assert hb == 1019

        fills = await s.get_fills("BTC-PERP")
        assert len(fills) == 20

        await s.close()
