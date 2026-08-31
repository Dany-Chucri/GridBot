"""Tests for OrderManager, diff computation, flip logic, ALO handling, and integration."""

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gridbot.config import AssetConfig, BotConfig
from gridbot.order_manager import OrderManager
from gridbot.types import (
    DesiredOrder,
    Fill,
    OpenOrder,
    OrderSide,
    Position,
    TimeInForce,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> BotConfig:
    return BotConfig(**overrides)


def _desired(
    client_order_id: str = "0x" + "a" * 32,
    symbol: str = "BTC-PERP",
    price: float = 50000.0,
    size: float = 0.1,
    side: OrderSide = OrderSide.BUY,
    tif: TimeInForce = TimeInForce.ALO,
    reduce_only: bool = False,
) -> DesiredOrder:
    return DesiredOrder(
        client_order_id=client_order_id,
        symbol=symbol,
        price=price,
        size=size,
        side=side,
        time_in_force=tif,
        reduce_only=reduce_only,
    )


def _open(
    order_id: int = 1,
    client_order_id: str = "0x" + "a" * 32,
    symbol: str = "BTC-PERP",
    price: float = 50000.0,
    size: float = 0.1,
    remaining: float = 0.1,
    side: OrderSide = OrderSide.BUY,
    reduce_only: bool = False,
) -> OpenOrder:
    return OpenOrder(
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        price=price,
        size=size,
        remaining=remaining,
        side=side,
        reduce_only=reduce_only,
    )


def _fill(
    fill_id: str = "f1",
    order_id: int = 1,
    client_order_id: str = "co1",
    symbol: str = "BTC-PERP",
    price: float = 50000.0,
    size: float = 0.1,
    side: OrderSide = OrderSide.BUY,
    fee: float = 0.0,
    timestamp_ms: int = 1000,
    is_maker: bool = True,
    is_partial: bool = False,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        price=price,
        size=size,
        side=side,
        fee=fee,
        timestamp_ms=timestamp_ms,
        is_maker=is_maker,
        is_partial=is_partial,
    )


def _pos(
    symbol: str = "BTC-PERP",
    size: float = 0.1,
    avg_entry: float = 50000.0,
    unrealized_pnl: float = 0.0,
) -> Position:
    return Position(
        symbol=symbol,
        size=size,
        avg_entry_price=avg_entry,
        unrealized_pnl=unrealized_pnl,
    )


def _om(config: BotConfig | None = None) -> OrderManager:
    """Create an OrderManager instance for testing (no exchange connection)."""
    return OrderManager(config or _cfg())


# ===========================================================================
# Test: Diff Computation (section 7.2), pure function, no mocks needed
# ===========================================================================


class TestOrderDiff:
    """Reconciliation diff computation (section 7.2)."""

    def test_no_diff_when_desired_matches_current(self):
        """No changes when desired exactly matches current."""
        om = _om()
        cloid = "0x" + "a" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0, size=0.1)]
        current = [_open(client_order_id=cloid, price=50000.0, size=0.1)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert to_place == []

    def test_new_order_to_place(self):
        """New desired order with no matching current -> to_place only."""
        om = _om()
        desired = [_desired(price=49000.0)]
        current = []
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert len(to_place) == 1
        assert to_place[0].price == 49000.0

    def test_stale_order_to_cancel(self):
        """Current order with no matching desired -> to_cancel only."""
        om = _om()
        desired = []
        current = [_open(order_id=42, price=51000.0)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert to_cancel[0].order_id == 42
        assert to_place == []

    def test_changed_size_cancel_and_place(self):
        """Same price/side but different size -> cancel old, place new."""
        om = _om()
        cloid = "0x" + "b" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0, size=0.2)]
        current = [_open(client_order_id=cloid, order_id=10, price=50000.0, size=0.1)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert to_cancel[0].order_id == 10
        assert len(to_place) == 1
        assert to_place[0].size == 0.2

    def test_changed_price_cancel_and_place(self):
        """Same side/size but different price -> cancel old, place new."""
        om = _om()
        cloid = "0x" + "c" * 32
        desired = [_desired(client_order_id=cloid, price=49500.0, size=0.1)]
        current = [_open(client_order_id=cloid, order_id=11, price=50000.0, size=0.1)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert len(to_place) == 1
        assert to_place[0].price == 49500.0

    def test_changed_reduce_only_cancel_and_place(self):
        """Same price/side/size but different reduce_only -> cancel old, place new."""
        om = _om()
        cloid = "0x" + "d" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0, reduce_only=True)]
        current = [_open(client_order_id=cloid, order_id=12, price=50000.0, reduce_only=False)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert len(to_place) == 1

    def test_mixed_scenario(self):
        """Mixed: some match, some cancel, some place."""
        om = _om()
        cloid_keep = "0x" + "1" * 32
        cloid_stale = "0x" + "2" * 32
        cloid_new = "0x" + "3" * 32

        desired = [
            _desired(client_order_id=cloid_keep, price=50000.0, size=0.1, side=OrderSide.BUY),
            _desired(client_order_id=cloid_new, price=48000.0, size=0.2, side=OrderSide.BUY),
        ]
        current = [
            _open(client_order_id=cloid_keep, order_id=1, price=50000.0, size=0.1, side=OrderSide.BUY),
            _open(client_order_id=cloid_stale, order_id=2, price=52000.0, size=0.1, side=OrderSide.SELL),
        ]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1  # stale order
        assert to_cancel[0].order_id == 2
        assert len(to_place) == 1  # new order
        assert to_place[0].price == 48000.0

    def test_multiple_matches(self):
        """Multiple orders all matching -> no changes."""
        om = _om()
        desired = [
            _desired(client_order_id="0x" + "a" * 32, price=49000.0, side=OrderSide.BUY),
            _desired(client_order_id="0x" + "b" * 32, price=51000.0, side=OrderSide.SELL),
        ]
        current = [
            _open(client_order_id="0x" + "a" * 32, order_id=1, price=49000.0, side=OrderSide.BUY),
            _open(client_order_id="0x" + "b" * 32, order_id=2, price=51000.0, side=OrderSide.SELL),
        ]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert to_place == []

    def test_signature_match_without_cloid(self):
        """Match by signature (price, side, size, reduce_only) when cloid differs."""
        om = _om()
        desired = [_desired(client_order_id="0x" + "x" * 32, price=50000.0, size=0.1, side=OrderSide.BUY)]
        current = [_open(client_order_id="0x" + "y" * 32, order_id=5, price=50000.0, size=0.1, side=OrderSide.BUY)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert to_place == []

    def test_empty_desired_cancels_all(self):
        """Empty desired set -> cancel all current orders."""
        om = _om()
        current = [
            _open(order_id=1, price=49000.0),
            _open(order_id=2, price=51000.0, side=OrderSide.SELL),
        ]
        to_cancel, to_place = om._compute_diff([], current)
        assert len(to_cancel) == 2
        assert to_place == []

    def test_empty_current_places_all(self):
        """Empty current set -> place all desired orders."""
        om = _om()
        desired = [
            _desired(price=49000.0),
            _desired(client_order_id="0x" + "b" * 32, price=51000.0, side=OrderSide.SELL),
        ]
        to_cancel, to_place = om._compute_diff(desired, [])
        assert to_cancel == []
        assert len(to_place) == 2

    def test_both_empty(self):
        """Both empty -> no changes."""
        om = _om()
        to_cancel, to_place = om._compute_diff([], [])
        assert to_cancel == []
        assert to_place == []

    def test_same_price_different_side(self):
        """Same price but different side -> not a match."""
        om = _om()
        desired = [_desired(client_order_id="0x" + "a" * 32, price=50000.0, side=OrderSide.SELL)]
        current = [_open(client_order_id="0x" + "b" * 32, order_id=1, price=50000.0, side=OrderSide.BUY)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert len(to_place) == 1


# ===========================================================================
# Test: Flip Orders (section 7.5), pure function, no mocks needed
# ===========================================================================


class TestFlipOrders:
    """Fill handling and grid flip logic (section 7.5)."""

    def test_buy_fill_produces_sell_flip(self):
        """Buy fill -> sell flip one step higher."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=50000.0, size=0.1)
        step_bps = 20.0  # 20 bps = 0.20%

        result = om.compute_flip_order(fill, step_bps, inventory_zone_is_hard_cap=False)

        assert result is not None
        assert result.side == OrderSide.SELL
        expected_price = 50000.0 * (1 + 20.0 / 10_000)
        assert abs(result.price - expected_price) < 0.01
        assert result.size == 0.1
        assert result.reduce_only is False

    def test_sell_fill_produces_buy_flip(self):
        """Sell fill -> buy flip one step lower."""
        om = _om()
        fill = _fill(side=OrderSide.SELL, price=50000.0, size=0.1)
        step_bps = 20.0

        result = om.compute_flip_order(fill, step_bps, inventory_zone_is_hard_cap=False)

        assert result is not None
        assert result.side == OrderSide.BUY
        expected_price = 50000.0 * (1 - 20.0 / 10_000)
        assert abs(result.price - expected_price) < 0.01
        assert result.size == 0.1

    def test_hard_cap_sets_reduce_only(self):
        """At hard cap: flip order has reduce_only=True."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=50000.0, size=0.1)

        result = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=True)

        assert result is not None
        assert result.reduce_only is True

    def test_not_hard_cap_no_reduce_only(self):
        """Not at hard cap: flip order has reduce_only=False."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=50000.0, size=0.1)

        result = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=False)

        assert result is not None
        assert result.reduce_only is False

    def test_partial_fill_returns_none(self):
        """Partial fill -> no flip (don't flip on partials)."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=50000.0, size=0.05, is_partial=True)

        result = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=False)

        assert result is None

    def test_deterministic_flip_id(self):
        """Flip order ID is deterministic based on fill data."""
        om = _om()
        fill = _fill(fill_id="fill_123", side=OrderSide.BUY, price=50000.0)

        result1 = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=False)
        result2 = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=False)

        assert result1 is not None
        assert result2 is not None
        assert result1.client_order_id == result2.client_order_id
        assert result1.client_order_id.startswith("0x")
        assert len(result1.client_order_id) == 34  # "0x" + 32 hex chars

    def test_flip_has_alo_tif(self):
        """Flip orders use ALO time-in-force."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=50000.0)

        result = om.compute_flip_order(fill, 20.0, inventory_zone_is_hard_cap=False)

        assert result is not None
        assert result.time_in_force == TimeInForce.ALO

    def test_flip_preserves_symbol(self):
        """Flip order uses same symbol as fill."""
        om = _om()
        fill = _fill(symbol="ETH-PERP", side=OrderSide.SELL, price=3000.0)

        result = om.compute_flip_order(fill, 15.0, inventory_zone_is_hard_cap=False)

        assert result is not None
        assert result.symbol == "ETH-PERP"

    def test_flip_step_calculation_precision(self):
        """Step calculation handles precision correctly."""
        om = _om()
        fill = _fill(side=OrderSide.BUY, price=100000.0, size=0.001)
        step_bps = 15.0  # 15 bps

        result = om.compute_flip_order(fill, step_bps, inventory_zone_is_hard_cap=False)

        assert result is not None
        expected = 100000.0 * (1 + 15.0 / 10_000)
        assert abs(result.price - expected) < 0.001

    def test_different_fills_produce_different_ids(self):
        """Different fills produce different flip order IDs."""
        om = _om()
        fill1 = _fill(fill_id="fill_1", side=OrderSide.BUY, price=50000.0)
        fill2 = _fill(fill_id="fill_2", side=OrderSide.BUY, price=50000.0)

        result1 = om.compute_flip_order(fill1, 20.0, False)
        result2 = om.compute_flip_order(fill2, 20.0, False)

        assert result1 is not None and result2 is not None
        assert result1.client_order_id != result2.client_order_id


# ===========================================================================
# Test: ALO Rejection Handling (section 7.4)
# ===========================================================================


class TestAloRejection:
    """Post-Only rejection handling (section 7.4)."""

    @pytest.mark.asyncio
    async def test_nudge_buy_lower(self):
        """Buy order nudged lower (farther from mid)."""
        om = _om()
        order = _desired(price=50000.0, side=OrderSide.BUY)

        result = await om._handle_alo_rejection(order, mid_price=50050.0, attempt=0)

        assert result is not None
        assert result.price == 50000.0 - 1.0  # BTC tick_size=1.0
        assert result.side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_nudge_sell_higher(self):
        """Sell order nudged higher (farther from mid)."""
        om = _om()
        order = _desired(price=51000.0, side=OrderSide.SELL)

        result = await om._handle_alo_rejection(order, mid_price=50950.0, attempt=0)

        assert result is not None
        assert result.price == 51000.0 + 1.0  # BTC tick_size=1.0
        assert result.side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """Returns None when max retries exceeded."""
        cfg = _cfg()
        cfg.assets[0].post_only_max_retries = 3
        om = _om(cfg)
        order = _desired(price=50000.0)

        result = await om._handle_alo_rejection(order, mid_price=50050.0, attempt=3)

        assert result is None

    @pytest.mark.asyncio
    async def test_retry_within_limit(self):
        """Returns adjusted order when within retry limit."""
        cfg = _cfg()
        cfg.assets[0].post_only_max_retries = 3
        om = _om(cfg)
        order = _desired(price=50000.0, side=OrderSide.BUY)

        result = await om._handle_alo_rejection(order, mid_price=50050.0, attempt=2)

        assert result is not None

    @pytest.mark.asyncio
    async def test_preserves_order_fields(self):
        """Nudged order preserves all fields except price."""
        om = _om()
        order = _desired(
            client_order_id="0x" + "f" * 32,
            symbol="ETH-PERP",
            price=3000.0,
            size=1.0,
            side=OrderSide.BUY,
            tif=TimeInForce.ALO,
            reduce_only=True,
        )

        result = await om._handle_alo_rejection(order, mid_price=3050.0, attempt=0)

        assert result is not None
        assert result.client_order_id == order.client_order_id
        assert result.symbol == order.symbol
        assert result.size == order.size
        assert result.side == order.side
        assert result.time_in_force == order.time_in_force
        assert result.reduce_only == order.reduce_only
        assert result.price != order.price  # price should change

    @pytest.mark.asyncio
    async def test_successive_nudges_accumulate(self):
        """Successive nudges move price farther from mid."""
        om = _om()
        order = _desired(price=50000.0, side=OrderSide.BUY)

        result1 = await om._handle_alo_rejection(order, mid_price=50050.0, attempt=0)
        assert result1 is not None

        result2 = await om._handle_alo_rejection(result1, mid_price=50050.0, attempt=1)
        assert result2 is not None

        assert result2.price < result1.price < order.price


# ===========================================================================
# Test: Deterministic Order IDs (section 7.3)
# ===========================================================================


class TestOrderIds:
    """Deterministic client order ID generation."""

    def test_deterministic(self):
        """Same inputs produce same ID."""
        id1 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        id2 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        assert id1 == id2

    def test_different_price_different_id(self):
        id1 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        id2 = OrderManager._generate_order_id("BTC-PERP", 49000.0, OrderSide.BUY, "hash1", 1)
        assert id1 != id2

    def test_different_side_different_id(self):
        id1 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        id2 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.SELL, "hash1", 1)
        assert id1 != id2

    def test_different_epoch_different_id(self):
        id1 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        id2 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 2)
        assert id1 != id2

    def test_different_config_hash_different_id(self):
        id1 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash1", 1)
        id2 = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "hash2", 1)
        assert id1 != id2

    def test_format_cloid_compatible(self):
        """ID format is Cloid-compatible: 0x + 32 hex chars."""
        oid = OrderManager._generate_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "h", 1)
        assert oid.startswith("0x")
        assert len(oid) == 34
        # Validate hex
        int(oid[2:], 16)

    def test_backstop_id_deterministic(self):
        id1 = OrderManager._generate_backstop_id("BTC-PERP", "long", "cfg1")
        id2 = OrderManager._generate_backstop_id("BTC-PERP", "long", "cfg1")
        assert id1 == id2

    def test_backstop_id_differs_by_direction(self):
        id1 = OrderManager._generate_backstop_id("BTC-PERP", "long", "cfg1")
        id2 = OrderManager._generate_backstop_id("BTC-PERP", "short", "cfg1")
        assert id1 != id2


# ===========================================================================
# Test: Coin/Symbol Conversion
# ===========================================================================


class TestHelpers:
    def test_to_coin_btc(self):
        assert OrderManager._to_coin("BTC-PERP") == "BTC"

    def test_to_coin_eth(self):
        assert OrderManager._to_coin("ETH-PERP") == "ETH"

    def test_tif_to_sdk_alo(self):
        assert OrderManager._tif_to_sdk(TimeInForce.ALO) == "Alo"

    def test_tif_to_sdk_ioc(self):
        assert OrderManager._tif_to_sdk(TimeInForce.IOC) == "Ioc"

    def test_tif_to_sdk_gtc(self):
        assert OrderManager._tif_to_sdk(TimeInForce.GTC) == "Gtc"


# ===========================================================================
# Test: Flatten Limit Price Computation
# ===========================================================================


class TestFlattenLimitPrice:
    def test_sell_limit_below_mid(self):
        """Sell IOC limit is below mid (negative slippage)."""
        price = OrderManager._compute_flatten_limit_price(50000.0, is_buy=False, slippage_bps=50.0)
        expected = 50000.0 * (1 - 50.0 / 10_000)
        assert abs(price - expected) < 0.01

    def test_buy_limit_above_mid(self):
        """Buy IOC limit is above mid (positive slippage)."""
        price = OrderManager._compute_flatten_limit_price(50000.0, is_buy=True, slippage_bps=50.0)
        expected = 50000.0 * (1 + 50.0 / 10_000)
        assert abs(price - expected) < 0.01

    def test_zero_slippage(self):
        """Zero slippage -> limit at mid."""
        price = OrderManager._compute_flatten_limit_price(50000.0, is_buy=True, slippage_bps=0.0)
        assert abs(price - 50000.0) < 0.01

    def test_eth_wider_slippage(self):
        """ETH flatten slippage is wider (75 bps vs 50 bps)."""
        btc_price = OrderManager._compute_flatten_limit_price(50000.0, False, 50.0)
        eth_price = OrderManager._compute_flatten_limit_price(3000.0, False, 75.0)
        # ETH should have proportionally wider slippage
        btc_slip = abs(50000.0 - btc_price) / 50000.0
        eth_slip = abs(3000.0 - eth_price) / 3000.0
        assert eth_slip > btc_slip


# ===========================================================================
# Test: ALO Rejection Tracking
# ===========================================================================


class TestAloTracking:
    def test_tracking_increments(self):
        om = _om()
        om._track_alo_rejection("BTC-PERP")
        assert om._alo_rejection_counts["BTC-PERP"] == 1
        om._track_alo_rejection("BTC-PERP")
        assert om._alo_rejection_counts["BTC-PERP"] == 2

    def test_separate_symbols(self):
        om = _om()
        om._track_alo_rejection("BTC-PERP")
        om._track_alo_rejection("ETH-PERP")
        assert om._alo_rejection_counts["BTC-PERP"] == 1
        assert om._alo_rejection_counts["ETH-PERP"] == 1

    def test_window_reset(self):
        om = _om()
        # Set window start far in the past
        om._alo_rejection_window_start_ms = 0
        om._alo_rejection_counts["BTC-PERP"] = 10
        om._track_alo_rejection("BTC-PERP")
        # Should have been reset (old count cleared) and new count = 1
        assert om._alo_rejection_counts["BTC-PERP"] == 1


# ===========================================================================
# Test: Batch Submission (integration with mock SDK)
# ===========================================================================


class TestBatchSubmission:
    """Integration tests for batch submission with mocked SDK."""

    @pytest.mark.asyncio
    async def test_submit_batch_cancels_then_places(self):
        """Cancels are sent before placements."""
        om = _om()
        om._client = MagicMock()
        call_order = []

        def mock_cancel(reqs):
            call_order.append("cancel")
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}

        def mock_place(reqs):
            call_order.append("place")
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        om._client.bulk_cancel = mock_cancel
        om._client.bulk_orders = mock_place

        cancels = [_open(order_id=1, symbol="BTC-PERP")]
        places = [_desired(symbol="BTC-PERP")]

        await om._submit_batch(cancels, places)

        assert call_order == ["cancel", "place"]

    @pytest.mark.asyncio
    async def test_submit_batch_cancels_only(self):
        """Only cancels when no placements."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}

        cancels = [_open(order_id=1)]
        await om._submit_batch(cancels, [])

        om._client.bulk_cancel.assert_called_once()
        om._client.bulk_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_submit_batch_places_only(self):
        """Only places when no cancels."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        places = [_desired()]
        await om._submit_batch([], places)

        om._client.bulk_cancel.assert_not_called()
        om._client.bulk_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_batch_cancel_failure_still_places(self):
        """Cancel failure does not prevent placement attempt."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_cancel.side_effect = Exception("network error")
        om._client.bulk_orders.return_value = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        cancels = [_open(order_id=1)]
        places = [_desired()]

        await om._submit_batch(cancels, places)

        om._client.bulk_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_batch_place_failure_propagates(self):
        """A batch-place exception (e.g. malformed request) is not swallowed.

        Unlike a per-order rejection reported in the response body, this
        means no order in the batch ever reached the exchange, and must
        surface to the caller so it counts toward the consecutive-error
        kill switch instead of retrying forever in silence.
        """
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.side_effect = ValueError(
            "float_to_wire causes rounding", 78573.3658405173
        )

        places = [_desired()]

        with pytest.raises(ValueError):
            await om._submit_batch([], places)

    @pytest.mark.asyncio
    async def test_submit_batch_rounds_price_to_tick(self):
        """Placement prices are snapped to tick_size before hitting the SDK.

        Grid/anchor arithmetic produces float noise (e.g. anchor * (1 - i *
        step_bps / 10_000)) that the Hyperliquid SDK's float_to_wire rejects
        outright if not rounded to a clean multiple of tick_size first.
        """
        om = _om()
        om._client = MagicMock()
        captured_args = []

        def mock_place(reqs):
            captured_args.extend(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_place

        order = _desired(symbol="BTC-PERP", price=78573.3658405173)
        await om._submit_batch([], [order])

        assert captured_args[0]["limit_px"] == 78573.0

    @pytest.mark.asyncio
    async def test_submit_batch_rounds_size_to_sz_decimals(self):
        """Placement sizes are snapped to szDecimals before hitting the SDK.

        Vol scaling and inventory skew produce arbitrary-precision sizes
        (e.g. an increasing-side buy shrunk near the soft cap) that the SDK's
        float_to_wire rejects, failing the whole batch. A full-size level is
        already clean, so this only bites once skew is active.
        """
        om = _om()
        om._client = MagicMock()
        captured_args = []

        def mock_place(reqs):
            captured_args.extend(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_place

        order = _desired(symbol="BTC-PERP", size=0.00015597843404667523)
        await om._submit_batch([], [order])

        assert captured_args[0]["sz"] == 0.00016

    @pytest.mark.asyncio
    async def test_submit_batch_builds_correct_order_request(self):
        """Placement request has correct format for Hyperliquid SDK."""
        om = _om()
        om._client = MagicMock()
        captured_args = []

        def mock_place(reqs):
            captured_args.extend(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_place

        order = _desired(
            symbol="ETH-PERP",
            price=3000.0,
            size=1.5,
            side=OrderSide.SELL,
            tif=TimeInForce.ALO,
            reduce_only=True,
        )
        await om._submit_batch([], [order])

        assert len(captured_args) == 1
        req = captured_args[0]
        assert req["coin"] == "ETH"
        assert req["is_buy"] is False
        assert req["sz"] == 1.5
        assert req["limit_px"] == 3000.0
        assert req["order_type"] == {"limit": {"tif": "Alo"}}
        assert req["reduce_only"] is True

    @pytest.mark.asyncio
    async def test_reconcile_no_diff_skips_batch(self):
        """Reconcile with no diff does not call exchange."""
        om = _om()
        om._client = MagicMock()

        cloid = "0x" + "a" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0)]
        current = [_open(client_order_id=cloid, price=50000.0)]

        await om.reconcile("BTC-PERP", desired, current, mid_price=50000.0)

        om._client.bulk_cancel.assert_not_called()
        om._client.bulk_orders.assert_not_called()


# ===========================================================================
# Test: Cancel All Orders (integration with mock SDK)
# ===========================================================================


class TestCancelAll:
    @pytest.mark.asyncio
    async def test_cancel_all_with_orders(self):
        """Cancels all orders for the specified symbol."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        om._info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1, "limitPx": "50000", "sz": "0.1", "side": "B"},
            {"coin": "BTC", "oid": 2, "limitPx": "51000", "sz": "0.1", "side": "A"},
            {"coin": "ETH", "oid": 3, "limitPx": "3000", "sz": "1.0", "side": "B"},
        ]
        om._client.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success", "success"]}}}

        await om.cancel_all_orders("BTC-PERP")

        om._client.bulk_cancel.assert_called_once()
        cancel_reqs = om._client.bulk_cancel.call_args[0][0]
        assert len(cancel_reqs) == 2  # Only BTC orders
        assert all(r["coin"] == "BTC" for r in cancel_reqs)

    @pytest.mark.asyncio
    async def test_cancel_all_no_orders(self):
        """No-op when no orders exist."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        om._info.open_orders.return_value = []

        await om.cancel_all_orders("BTC-PERP")

        om._client.bulk_cancel.assert_not_called()


# ===========================================================================
# Test: Emergency Flatten Protocol (integration with mock SDK)
# ===========================================================================


class TestFlattenProtocol:
    @pytest.mark.asyncio
    async def test_flatten_zero_position_returns_true(self):
        """No-op flatten when position is zero."""
        om = _om()
        pos = _pos(size=0.0)
        config = AssetConfig()

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=AsyncMock(), get_book_depth=AsyncMock(), get_position=AsyncMock(),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_flatten_long_sends_sell_ioc(self):
        """Flatten long position sends SELL IOC orders."""
        om = _om()
        om._client = MagicMock()
        captured = []

        def mock_orders(reqs):
            captured.extend(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_orders

        pos = _pos(size=0.5)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=1.0,
            flatten_retry_pause_ms=100.0,
        )

        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(return_value=1.0)  # enough depth
        get_pos = AsyncMock(return_value=_pos(size=0.0))  # fully flattened after first IOC

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=get_pos,
        )

        assert result is True
        assert len(captured) >= 1
        assert captured[0]["is_buy"] is False  # SELL to close long
        assert captured[0]["reduce_only"] is True
        assert captured[0]["order_type"] == {"limit": {"tif": "Ioc"}}

    @pytest.mark.asyncio
    async def test_flatten_short_sends_buy_ioc(self):
        """Flatten short position sends BUY IOC orders."""
        om = _om()
        om._client = MagicMock()
        captured = []

        def mock_orders(reqs):
            captured.extend(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_orders

        pos = _pos(size=-0.3)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=1.0,
            flatten_retry_pause_ms=100.0,
        )

        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(return_value=1.0)
        get_pos = AsyncMock(return_value=_pos(size=0.0))

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=get_pos,
        )

        assert result is True
        assert captured[0]["is_buy"] is True  # BUY to close short

    @pytest.mark.asyncio
    async def test_flatten_returns_false_on_residual(self):
        """Returns False when position can't be fully flattened."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {"status": "ok", "response": {"type": "order", "data": {"statuses": []}}}

        pos = _pos(size=1.0)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=0.5,
            flatten_retry_pause_ms=50.0,
        )

        # Position never changes (fills don't execute)
        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(return_value=0.5)
        get_pos = AsyncMock(return_value=_pos(size=1.0))

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=get_pos,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_flatten_depth_check_failure_proceeds(self):
        """Flatten proceeds even if depth check fails."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}}}

        pos = _pos(size=0.1)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=1.0,
            flatten_retry_pause_ms=100.0,
        )

        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(side_effect=Exception("WS disconnected"))
        get_pos = AsyncMock(return_value=_pos(size=0.0))

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=get_pos,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_flatten_uses_chunked_tranches(self):
        """Uses chunked tranches when depth < position size."""
        om = _om()
        om._client = MagicMock()
        sent_sizes = []

        def mock_orders(reqs):
            sent_sizes.append(reqs[0]["sz"])
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_orders

        pos = _pos(size=1.0)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=2.0,
            flatten_retry_pause_ms=50.0,
            flatten_tranche_pct=0.8,
        )

        call_count = [0]

        async def mock_get_pos(symbol):
            call_count[0] += 1
            if call_count[0] >= 2:
                return _pos(size=0.0)
            return _pos(size=0.5)

        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(return_value=0.5)  # Only 0.5 depth available

        await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=mock_get_pos,
        )

        # First tranche should be depth * tranche_pct = 0.5 * 0.8 = 0.4
        assert abs(sent_sizes[0] - 0.4) < 0.01


# ===========================================================================
# Test: Backstop Stop-Loss (integration with mock SDK)
# ===========================================================================


class TestBackstop:
    @pytest.mark.asyncio
    async def test_backstop_cancelled_at_zero_position(self):
        """Backstop is cancelled when position is zero."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []

        pos = _pos(size=0.0)
        await om.update_backstop("BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0)

        # Should not place any new order
        om._client.bulk_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_backstop_long_position_sell_stop(self):
        """Long position -> sell stop-loss below anchor."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []
        om._client.bulk_orders.return_value = {"status": "ok"}

        pos = _pos(size=0.5)
        anchor = 50000.0
        atr = 500.0
        breakout_atr = 4.5
        backstop_buffer = 1.0

        await om.update_backstop("BTC-PERP", pos, anchor, atr, breakout_atr, backstop_buffer)

        om._client.bulk_orders.assert_called_once()
        req = om._client.bulk_orders.call_args[0][0][0]
        assert req["is_buy"] is False  # Sell stop for long position
        assert req["reduce_only"] is True
        assert req["sz"] == 0.5
        expected_trigger = anchor - (breakout_atr + backstop_buffer) * atr
        assert abs(req["order_type"]["trigger"]["triggerPx"] - expected_trigger) < 0.01
        assert req["order_type"]["trigger"]["isMarket"] is True
        assert req["order_type"]["trigger"]["tpsl"] == "sl"

    @pytest.mark.asyncio
    async def test_backstop_short_position_buy_stop(self):
        """Short position -> buy stop-loss above anchor."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []
        om._client.bulk_orders.return_value = {"status": "ok"}

        pos = _pos(size=-0.3)
        anchor = 50000.0
        atr = 500.0

        await om.update_backstop("BTC-PERP", pos, anchor, atr, 4.5, 1.0)

        req = om._client.bulk_orders.call_args[0][0][0]
        assert req["is_buy"] is True  # Buy stop for short position
        expected_trigger = anchor + (4.5 + 1.0) * atr
        assert abs(req["order_type"]["trigger"]["triggerPx"] - expected_trigger) < 0.01

    @pytest.mark.asyncio
    async def test_backstop_cancels_existing_before_placing(self):
        """Existing backstop is cancelled before placing new one."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        # Existing trigger order with matching cloid
        expected_cloid = OrderManager._generate_backstop_id("BTC-PERP", "long", "")
        om._info.frontend_open_orders.return_value = [
            {"coin": "BTC", "oid": 99, "cloid": expected_cloid}
        ]
        om._client.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        om._client.bulk_orders.return_value = {"status": "ok"}

        pos = _pos(size=0.5)
        await om.update_backstop("BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0)

        # Should cancel the existing backstop
        om._client.bulk_cancel.assert_called_once()
        cancel_req = om._client.bulk_cancel.call_args[0][0]
        assert cancel_req[0]["oid"] == 99
        # And then place a new one
        om._client.bulk_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_backstop_cancels_stale_opposite_direction_on_flip(self):
        """If position flipped sign since the last update, the OLD
        direction's backstop (different cloid) must also be cancelled, not
        just the current direction's, otherwise it's orphaned."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        long_cloid = OrderManager._generate_backstop_id("BTC-PERP", "long", "")
        short_cloid = OrderManager._generate_backstop_id("BTC-PERP", "short", "")
        # A stale LONG backstop is still resting on the exchange even though
        # the position is now SHORT.
        om._info.frontend_open_orders.return_value = [
            {"coin": "BTC", "oid": 42, "cloid": long_cloid}
        ]
        om._client.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
        om._client.bulk_orders.return_value = {"status": "ok"}

        pos = _pos(size=-0.5)  # now short
        await om.update_backstop("BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0)

        # Two cancel lookups happen (current direction, then opposite); the
        # stale long-direction order must actually get cancelled.
        cancelled_oids = [
            req["oid"]
            for call in om._client.bulk_cancel.call_args_list
            for req in call[0][0]
        ]
        assert 42 in cancelled_oids


# ===========================================================================
# Test: Parse Results
# ===========================================================================


class TestParseResults:
    def test_parse_cancel_success(self):
        """Successful cancel result parsed without errors."""
        om = _om()
        result = {
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": ["success", "success"]}},
        }
        # Should not raise
        om._parse_cancel_result(result, [_open(order_id=1), _open(order_id=2)])

    def test_parse_cancel_partial_failure(self):
        """Partial cancel failure logged."""
        om = _om()
        result = {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": ["success", {"error": "order not found"}]},
            },
        }
        # Should not raise
        om._parse_cancel_result(result, [_open(order_id=1), _open(order_id=2)])

    def test_parse_cancel_error(self):
        """Cancel batch error logged."""
        om = _om()
        result = {"status": "err", "response": "rate limited"}
        om._parse_cancel_result(result, [_open(order_id=1)])

    def test_parse_cancel_unexpected_type(self):
        """Unexpected result type handled gracefully."""
        om = _om()
        om._parse_cancel_result("not a dict", [])  # type: ignore


# ===========================================================================
# Test: Build Order Type
# ===========================================================================


class TestBuildOrderType:
    def test_alo_order_type(self):
        om = _om()
        order = _desired(tif=TimeInForce.ALO)
        assert om._build_order_type(order) == {"limit": {"tif": "Alo"}}

    def test_ioc_order_type(self):
        om = _om()
        order = _desired(tif=TimeInForce.IOC)
        assert om._build_order_type(order) == {"limit": {"tif": "Ioc"}}

    def test_gtc_order_type(self):
        om = _om()
        order = _desired(tif=TimeInForce.GTC)
        assert om._build_order_type(order) == {"limit": {"tif": "Gtc"}}


# ===========================================================================
# Test: ALO Rejection Wiring (fix #1, _handle_alo_rejection actually called)
# ===========================================================================


class TestAloRetryWiring:
    """Verify ALO rejections trigger nudge-and-retry in _submit_batch."""

    @pytest.mark.asyncio
    async def test_alo_rejection_triggers_retry_with_nudged_price(self):
        """ALO rejection causes a second bulk_orders call with nudged price."""
        om = _om()
        om._client = MagicMock()
        call_count = [0]
        captured_batches = []

        def mock_place(reqs):
            call_count[0] += 1
            captured_batches.append(reqs)
            if call_count[0] == 1:
                # First call: ALO rejection
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {"statuses": [{"error": "BadAloPx"}]},
                    },
                }
            # Retry: success
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 2}}]},
                },
            }

        om._client.bulk_orders = mock_place

        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=50050.0)

        assert call_count[0] == 2
        # Retry should have a lower price (nudged farther from mid for buy)
        retry_price = captured_batches[1][0]["limit_px"]
        assert retry_price == 50000.0 - 1.0  # BTC tick_size=1.0

    @pytest.mark.asyncio
    async def test_alo_rejection_no_retry_without_mid_price(self):
        """No retry when mid_price is not provided (0.0)."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "BadAloPx"}]},
            },
        }

        order = _desired(price=50000.0, side=OrderSide.BUY)
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=0.0)

        # Only one call, no retry
        om._client.bulk_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_alo_rejection_exhausted_retries_no_retry(self):
        """No retry when max retries already exhausted."""
        cfg = _cfg()
        cfg.assets[0].post_only_max_retries = 0  # No retries allowed
        om = _om(cfg)
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "BadAloPx"}]},
            },
        }

        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=50050.0)

        om._client.bulk_orders.assert_called_once()


# ===========================================================================
# Test: Per-Symbol Config Resolution (fix #2)
# ===========================================================================


class TestAssetConfigResolution:
    """Verify _get_asset_config resolves by symbol, not hardcoded index."""

    def test_resolves_btc_config(self):
        om = _om()
        cfg = om._get_asset_config("BTC-PERP")
        assert cfg.symbol == "BTC-PERP"

    def test_resolves_eth_config(self):
        om = _om()
        cfg = om._get_asset_config("ETH-PERP")
        assert cfg.symbol == "ETH-PERP"

    def test_unknown_symbol_falls_back_to_first(self):
        om = _om()
        cfg = om._get_asset_config("SOL-PERP")
        assert cfg.symbol == "BTC-PERP"  # first in list

    @pytest.mark.asyncio
    async def test_alo_rejection_uses_correct_asset_retries(self):
        """ETH order uses ETH config's post_only_max_retries."""
        from gridbot.config import default_eth_config

        eth_cfg = default_eth_config()
        eth_cfg.post_only_max_retries = 5  # Different from BTC default of 3
        btc_cfg = AssetConfig(post_only_max_retries=1)
        cfg = _cfg(assets=[btc_cfg, eth_cfg])
        om = _om(cfg)

        # BTC with attempt=1 should be exhausted (max=1)
        btc_order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        result_btc = await om._handle_alo_rejection(btc_order, 50050.0, attempt=1)
        assert result_btc is None

        # ETH with attempt=1 should still have retries left (max=5)
        eth_order = _desired(price=3000.0, side=OrderSide.BUY, symbol="ETH-PERP")
        result_eth = await om._handle_alo_rejection(eth_order, 3050.0, attempt=1)
        assert result_eth is not None


# ===========================================================================
# Test: Duplicate Signature Matching (fix #5)
# ===========================================================================


class TestDuplicateSignatureMatching:
    """Verify _compute_diff handles multiple current orders with same signature."""

    def test_two_current_orders_same_signature_both_matched(self):
        """Two current orders with same (price, side, size, reduce_only) can
        both match two desired orders with the same signature."""
        om = _om()
        desired = [
            _desired(client_order_id="0x" + "a" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
            _desired(client_order_id="0x" + "b" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        current = [
            _open(client_order_id="0x" + "c" * 32, order_id=1, price=50000.0, size=0.1, side=OrderSide.BUY),
            _open(client_order_id="0x" + "d" * 32, order_id=2, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        to_cancel, to_place = om._compute_diff(desired, current)
        # Both should match by signature, no cancels, no places
        assert to_cancel == []
        assert to_place == []

    def test_three_current_two_desired_one_cancelled(self):
        """Three current with same sig, two desired -> one cancel."""
        om = _om()
        desired = [
            _desired(client_order_id="0x" + "a" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
            _desired(client_order_id="0x" + "b" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        current = [
            _open(client_order_id="0x" + "c" * 32, order_id=1, price=50000.0, size=0.1, side=OrderSide.BUY),
            _open(client_order_id="0x" + "d" * 32, order_id=2, price=50000.0, size=0.1, side=OrderSide.BUY),
            _open(client_order_id="0x" + "e" * 32, order_id=3, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert to_place == []

    def test_one_current_two_desired_one_placed(self):
        """One current with sig, two desired same sig -> one place."""
        om = _om()
        desired = [
            _desired(client_order_id="0x" + "a" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
            _desired(client_order_id="0x" + "b" * 32, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        current = [
            _open(client_order_id="0x" + "c" * 32, order_id=1, price=50000.0, size=0.1, side=OrderSide.BUY),
        ]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert len(to_place) == 1


# ===========================================================================
# Test: Flatten Depth Call Argument Order (fix #6)
# ===========================================================================


class TestFlattenDepthArgOrder:
    """Verify flatten passes (symbol, depth_bps, side) in correct order."""

    @pytest.mark.asyncio
    async def test_flatten_depth_call_arg_order(self):
        """get_book_depth receives (symbol, slippage_bps, close_side)."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}},
        }

        pos = _pos(size=0.5)  # Long -> close_side = SELL
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=1.0,
            flatten_retry_pause_ms=100.0,
        )

        depth_args = []

        async def mock_depth(*args, **kwargs):
            depth_args.append(args)
            return 1.0

        get_mid = AsyncMock(return_value=50000.0)
        get_pos = AsyncMock(return_value=_pos(size=0.0))

        await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=mock_depth, get_position=get_pos,
        )

        assert len(depth_args) >= 1
        sym, bps, side = depth_args[0]
        assert sym == "BTC-PERP"
        assert bps == 50.0  # max_flatten_slippage_bps
        assert side == OrderSide.SELL  # close_side for long


# ===========================================================================
# Test: Backstop Config Hash (fix #4)
# ===========================================================================


class TestBackstopConfigHash:
    """Verify backstop uses caller-provided config_hash for ID generation."""

    @pytest.mark.asyncio
    async def test_backstop_uses_provided_config_hash(self):
        """Backstop order ID is deterministic from config_hash, not anchor/ATR."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []
        om._client.bulk_orders.return_value = {"status": "ok"}

        pos = _pos(size=0.5)
        # Same config_hash but different ATR -> same backstop ID
        expected_id = OrderManager._generate_backstop_id("BTC-PERP", "long", "myhash123")

        await om.update_backstop(
            "BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0, config_hash="myhash123"
        )
        req1 = om._client.bulk_orders.call_args[0][0][0]

        om._client.reset_mock()
        om._info.frontend_open_orders.return_value = []

        await om.update_backstop(
            "BTC-PERP", pos, 50000.0, 600.0, 4.5, 1.0, config_hash="myhash123"
        )
        req2 = om._client.bulk_orders.call_args[0][0][0]

        # Both should use the same cloid since config_hash is the same
        assert str(req1["cloid"]) == str(req2["cloid"])


# ===========================================================================
# Test: ALO multi-retry loop (review fix #1)
# ===========================================================================


class TestAloMultiRetry:
    """Verify ALO rejection retries up to post_only_max_retries."""

    @pytest.mark.asyncio
    async def test_retries_up_to_max(self):
        """Multiple ALO rejections retry with incrementing attempts."""
        cfg = _cfg()
        cfg.assets[0].post_only_max_retries = 3
        om = _om(cfg)
        om._client = MagicMock()
        call_count = [0]
        captured_prices = []

        def mock_place(reqs):
            call_count[0] += 1
            captured_prices.append(reqs[0]["limit_px"])
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "BadAloPx"}]},
                },
            }

        om._client.bulk_orders = mock_place

        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=50050.0)

        # initial + 3 retries = 4 calls total
        assert call_count[0] == 4
        # Each retry nudges price down by tick_size=1.0
        assert captured_prices == [50000.0, 49999.0, 49998.0, 49997.0]

    @pytest.mark.asyncio
    async def test_retry_stops_on_success(self):
        """Retry loop exits early when a retry succeeds."""
        om = _om()
        om._client = MagicMock()
        call_count = [0]

        def mock_place(reqs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "status": "ok",
                    "response": {
                        "type": "order",
                        "data": {"statuses": [{"error": "BadAloPx"}]},
                    },
                }
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"resting": {"oid": 1}}]},
                },
            }

        om._client.bulk_orders = mock_place

        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=50050.0)

        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_sell_retries_nudge_up(self):
        """Sell ALO retries nudge price upward."""
        cfg = _cfg()
        cfg.assets[0].post_only_max_retries = 2
        om = _om(cfg)
        om._client = MagicMock()
        captured_prices = []

        def mock_place(reqs):
            captured_prices.append(reqs[0]["limit_px"])
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"error": "BadAloPx"}]},
                },
            }

        om._client.bulk_orders = mock_place

        order = _desired(price=51000.0, side=OrderSide.SELL, symbol="BTC-PERP")
        await om._submit_batch([], [order], symbol="BTC-PERP", mid_price=50950.0)

        # initial + 2 retries
        assert captured_prices == [51000.0, 51001.0, 51002.0]


# ===========================================================================
# Test: Flatten continues on transient errors (review fix #2)
# ===========================================================================


class TestFlattenTransientErrors:
    @pytest.mark.asyncio
    async def test_continues_on_mid_price_failure(self):
        """Flatten retries after get_mid_price fails, doesn't abort."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}},
        }

        pos = _pos(size=0.5)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=2.0,
            flatten_retry_pause_ms=50.0,
        )

        mid_call_count = [0]

        async def flaky_mid(symbol):
            mid_call_count[0] += 1
            if mid_call_count[0] == 1:
                raise Exception("WS blip")
            return 50000.0

        get_pos = AsyncMock(return_value=_pos(size=0.0))
        get_depth = AsyncMock(return_value=1.0)

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=flaky_mid, get_book_depth=get_depth, get_position=get_pos,
        )

        assert result is True
        assert mid_call_count[0] >= 2  # retried after failure

    @pytest.mark.asyncio
    async def test_continues_on_get_position_failure(self):
        """Flatten retries after get_position fails, doesn't abort."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}},
        }

        pos = _pos(size=0.5)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=2.0,
            flatten_retry_pause_ms=50.0,
        )

        pos_call_count = [0]

        async def flaky_pos(symbol):
            pos_call_count[0] += 1
            if pos_call_count[0] == 1:
                raise Exception("network error")
            return _pos(size=0.0)

        get_mid = AsyncMock(return_value=50000.0)
        get_depth = AsyncMock(return_value=1.0)

        result = await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=get_depth, get_position=flaky_pos,
        )

        assert result is True
        assert pos_call_count[0] >= 2


# ===========================================================================
# Test: Result status checking (review fix #3)
# ===========================================================================


class TestResultChecking:
    def test_check_bulk_result_ok(self):
        assert OrderManager._check_bulk_result({"status": "ok"}, "test", "BTC") is True

    def test_check_bulk_result_err(self):
        assert OrderManager._check_bulk_result(
            {"status": "err", "response": "rate limited"}, "test", "BTC"
        ) is False

    def test_check_bulk_result_non_dict(self):
        assert OrderManager._check_bulk_result("not a dict", "test", "BTC") is False

    @pytest.mark.asyncio
    async def test_backstop_logs_error_on_rejection(self):
        """Backstop doesn't log success when exchange returns error."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []
        om._client.bulk_orders.return_value = {
            "status": "err",
            "response": "insufficient margin",
        }

        pos = _pos(size=0.5)
        # Should not raise, just log the error
        await om.update_backstop("BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0)


# ===========================================================================
# Test: Per-asset tick size (review fix #4)
# ===========================================================================


class TestPerAssetTickSize:
    @pytest.mark.asyncio
    async def test_btc_uses_btc_tick_size(self):
        """BTC nudge uses tick_size=1.0."""
        om = _om()
        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        result = await om._handle_alo_rejection(order, 50050.0, attempt=0)
        assert result is not None
        assert result.price == 49999.0  # 50000 - 1.0

    @pytest.mark.asyncio
    async def test_eth_uses_eth_tick_size(self):
        """ETH nudge uses tick_size=0.01."""
        om = _om()
        order = _desired(price=3000.0, side=OrderSide.BUY, symbol="ETH-PERP")
        result = await om._handle_alo_rejection(order, 3050.0, attempt=0)
        assert result is not None
        assert abs(result.price - 2999.99) < 1e-9  # 3000 - 0.01

    @pytest.mark.asyncio
    async def test_custom_tick_size(self):
        """Custom tick_size in config is respected."""
        cfg = _cfg()
        cfg.assets[0].tick_size = 0.5
        om = _om(cfg)
        order = _desired(price=50000.0, side=OrderSide.BUY, symbol="BTC-PERP")
        result = await om._handle_alo_rejection(order, 50050.0, attempt=0)
        assert result is not None
        assert result.price == 49999.5


# ===========================================================================
# Test: Backstop cloid-based cancel (review fix #5)
# ===========================================================================


class TestBackstopCloidCancel:
    @pytest.mark.asyncio
    async def test_cancel_ignores_non_matching_cloid(self):
        """Backstop cancel only targets orders with matching cloid."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        om._info.frontend_open_orders.return_value = [
            {"coin": "BTC", "oid": 1, "cloid": "0x_unrelated_tp_order"},
            {"coin": "BTC", "oid": 2, "cloid": "0x_different"},
        ]

        await om._cancel_existing_backstop("BTC-PERP", expected_cloid="0xmy_backstop_cloid")

        om._client.bulk_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_position_cancels_both_directions(self):
        """Zero position cancels backstops for both long and short cloids."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"

        long_cloid = OrderManager._generate_backstop_id("BTC-PERP", "long", "cfg")
        short_cloid = OrderManager._generate_backstop_id("BTC-PERP", "short", "cfg")

        om._info.frontend_open_orders.return_value = [
            {"coin": "BTC", "oid": 10, "cloid": long_cloid},
            {"coin": "BTC", "oid": 20, "cloid": short_cloid},
        ]
        om._client.bulk_cancel.return_value = {
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": ["success"]}},
        }

        pos = _pos(size=0.0)
        await om.update_backstop("BTC-PERP", pos, 50000.0, 500.0, 4.5, 1.0, config_hash="cfg")

        assert om._client.bulk_cancel.call_count == 2


# ===========================================================================
# Test: Backstop integrated into batch (review fix #6)
# ===========================================================================


class TestReconcileWithBackstop:
    @pytest.mark.asyncio
    async def test_grid_and_backstop_in_same_batch(self):
        """reconcile_with_backstop sends grid + backstop in same cancel/place calls."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []
        cancel_calls = []
        place_calls = []

        def mock_cancel(reqs):
            cancel_calls.append(reqs)
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"] * len(reqs)}}}

        def mock_place(reqs):
            place_calls.append(reqs)
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": i}} for i in range(len(reqs))]}}}

        om._client.bulk_cancel = mock_cancel
        om._client.bulk_orders = mock_place

        stale = _open(order_id=99, client_order_id="0x" + "z" * 32, price=49000.0)
        desired = [_desired(client_order_id="0x" + "b" * 32, price=50000.0)]
        current = [stale]
        pos = _pos(size=0.5)

        await om.reconcile_with_backstop(
            "BTC-PERP", desired, current, mid_price=50000.0,
            position=pos, anchor=50000.0, atr=500.0,
            breakout_atr_distance=4.5, backstop_buffer_atr=1.0, config_hash="test",
        )

        # Grid cancel + backstop cancel in one batch call
        assert len(cancel_calls) == 1
        # Grid placement + backstop placement in one batch call
        assert len(place_calls) >= 1
        all_placed = place_calls[0]
        has_trigger = any("trigger" in str(r.get("order_type", "")) for r in all_placed)
        assert has_trigger

    @pytest.mark.asyncio
    async def test_rejected_backstop_leg_is_logged(self, caplog):
        """A rejected backstop leg must be surfaced even though the overall
        batch status is 'ok', this was previously silently swallowed
        because the status-parsing loop stopped at len(grid placements)."""
        om = _om()
        om._client = MagicMock()
        om._info = MagicMock()
        om._wallet_address = "0xtest"
        om._info.frontend_open_orders.return_value = []

        om._client.bulk_cancel = MagicMock(
            return_value={"status": "ok", "response": {"type": "cancel", "data": {"statuses": []}}}
        )

        def mock_place(reqs):
            # Grid order succeeds, backstop (2nd request) is rejected.
            statuses = [{"resting": {"oid": 1}}, {"error": "InsufficientMargin"}]
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}

        om._client.bulk_orders = mock_place

        desired = [_desired(client_order_id="0x" + "b" * 32, price=50000.0)]
        pos = _pos(size=0.5)

        with caplog.at_level("WARNING"):
            await om.reconcile_with_backstop(
                "BTC-PERP", desired, [], mid_price=50000.0,
                position=pos, anchor=50000.0, atr=500.0,
                breakout_atr_distance=4.5, backstop_buffer_atr=1.0, config_hash="test",
            )

        assert any("Backstop order update failed" in r.message for r in caplog.records)


# ===========================================================================
# Test: Structured ALO error matching (review fix #7)
# ===========================================================================


class TestAloErrorMatching:
    def test_dict_error_bad_alo_px(self):
        assert OrderManager._is_alo_rejection({"error": "BadAloPx"}) is True

    def test_dict_error_filled_immediately(self):
        assert OrderManager._is_alo_rejection(
            {"error": "Post only order would have been filled immediately."}
        ) is True

    def test_dict_non_alo_error(self):
        assert OrderManager._is_alo_rejection({"error": "InsufficientMargin"}) is False

    def test_string_bad_alo_px(self):
        assert OrderManager._is_alo_rejection("BadAloPx") is True

    def test_string_no_match(self):
        assert OrderManager._is_alo_rejection("some other error") is False

    def test_dict_resting_is_not_rejection(self):
        assert OrderManager._is_alo_rejection({"resting": {"oid": 1}}) is False

    def test_is_order_success_resting(self):
        assert OrderManager._is_order_success({"resting": {"oid": 1}}) is True

    def test_is_order_success_filled(self):
        assert OrderManager._is_order_success({"filled": {"oid": 1}}) is True

    def test_is_order_success_error(self):
        assert OrderManager._is_order_success({"error": "BadAloPx"}) is False


# ===========================================================================
# Test: mid_price warning (review fix #8)
# ===========================================================================


class TestMidPriceWarning:
    @pytest.mark.asyncio
    async def test_reconcile_warns_on_zero_mid_price(self, caplog):
        """Reconcile logs warning when mid_price=0 and there are desired orders."""
        om = _om()
        om._client = MagicMock()
        om._client.bulk_orders.return_value = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        desired = [_desired()]

        import logging
        with caplog.at_level(logging.WARNING):
            await om.reconcile("BTC-PERP", desired, [], mid_price=0.0)

        assert any("ALO retries will be suppressed" in m for m in caplog.messages)


# ===========================================================================
# Test: Tranche refresh in flatten (review fix #9)
# ===========================================================================


class TestTrancheRefresh:
    @pytest.mark.asyncio
    async def test_tranche_grows_when_depth_increases(self):
        """Tranche size updates if book depth increases between iterations."""
        om = _om()
        om._client = MagicMock()
        sent_sizes = []

        def mock_orders(reqs):
            sent_sizes.append(reqs[0]["sz"])
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 1}}]}}}

        om._client.bulk_orders = mock_orders

        pos = _pos(size=2.0)
        config = AssetConfig(
            max_flatten_slippage_bps=50.0,
            flatten_time_budget_seconds=3.0,
            flatten_retry_pause_ms=50.0,
            flatten_tranche_pct=0.8,
        )

        pos_call_count = [0]
        depth_call_count = [0]

        async def mock_pos(symbol):
            pos_call_count[0] += 1
            if pos_call_count[0] >= 3:
                return _pos(size=0.0)
            return _pos(size=1.0)

        async def mock_depth(symbol, bps, side):
            depth_call_count[0] += 1
            if depth_call_count[0] == 1:
                return 0.5  # Initial: small depth
            return 2.0  # Refreshed: much deeper book

        get_mid = AsyncMock(return_value=50000.0)

        await om.execute_flatten(
            "BTC-PERP", pos, config,
            get_mid_price=get_mid, get_book_depth=mock_depth, get_position=mock_pos,
        )

        # First tranche: min(2.0, 0.5*0.8) = 0.4
        assert abs(sent_sizes[0] - 0.4) < 0.01
        # Second tranche should be larger because depth refreshed to 2.0
        if len(sent_sizes) > 1:
            assert sent_sizes[1] > 0.4


# ===========================================================================
# Test: Relative tolerance in diff (review fix #10)
# ===========================================================================


class TestDiffRelativeTolerance:
    def test_close_prices_match(self):
        """Prices within rel_tol=1e-6 are considered matching."""
        om = _om()
        cloid = "0x" + "a" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0, size=0.1)]
        # Simulate a tiny float drift from exchange
        current = [_open(client_order_id=cloid, price=50000.00004, size=0.1)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert to_cancel == []
        assert to_place == []

    def test_different_prices_dont_match(self):
        """Prices differing by more than rel_tol=1e-6 are not matched."""
        om = _om()
        cloid = "0x" + "a" * 32
        desired = [_desired(client_order_id=cloid, price=50000.0, size=0.1)]
        current = [_open(client_order_id=cloid, price=50001.0, size=0.1)]
        to_cancel, to_place = om._compute_diff(desired, current)
        assert len(to_cancel) == 1
        assert len(to_place) == 1


# ===========================================================================
# Test: _build_place_request helper (review fix #11)
# ===========================================================================


class TestBuildPlaceRequest:
    def test_builds_correct_request(self):
        om = _om()
        order = _desired(
            symbol="ETH-PERP", price=3000.0, size=1.5,
            side=OrderSide.SELL, tif=TimeInForce.ALO, reduce_only=True,
        )
        req = om._build_place_request(order)
        assert req["coin"] == "ETH"
        assert req["is_buy"] is False
        assert req["sz"] == 1.5
        assert req["limit_px"] == 3000.0
        assert req["order_type"] == {"limit": {"tif": "Alo"}}
        assert req["reduce_only"] is True

    def test_includes_cloid_when_present(self):
        om = _om()
        cloid = "0x" + "f" * 32
        order = _desired(client_order_id=cloid)
        req = om._build_place_request(order)
        assert "cloid" in req

    def test_omits_cloid_when_empty(self):
        om = _om()
        order = _desired(client_order_id="")
        req = om._build_place_request(order)
        assert "cloid" not in req
