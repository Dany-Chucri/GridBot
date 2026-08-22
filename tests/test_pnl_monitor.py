"""Tests for PnLMonitor, PnL tracking, funding, and exchange cross-check."""

import pytest

from gridbot.config import BotConfig, PortfolioConfig
from gridbot.pnl_monitor import PnLMonitor
from gridbot.types import Fill, OrderSide, Position


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(
    divergence_threshold: float = 50.0,
    crosscheck_interval: float = 60.0,
) -> BotConfig:
    return BotConfig(
        portfolio=PortfolioConfig(
            pnl_divergence_threshold_usd=divergence_threshold,
            pnl_crosscheck_interval_seconds=crosscheck_interval,
        ),
    )


def _fill(
    symbol: str = "BTC-PERP",
    price: float = 50000.0,
    size: float = 0.1,
    side: OrderSide = OrderSide.BUY,
    fill_id: str = "f1",
    order_id: int = 1,
    client_order_id: str = "co1",
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
    unrealized_pnl: float = 100.0,
) -> Position:
    return Position(
        symbol=symbol,
        size=size,
        avg_entry_price=avg_entry,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# 5.1 Fill processing
# ---------------------------------------------------------------------------

class TestRecordFill:
    """record_fill updates realized PnL using average-cost method."""

    def test_opening_buy_no_realized_pnl(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY))
        assert mon.get_realized_pnl("BTC-PERP") == 0.0

    def test_opening_sell_no_realized_pnl(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.SELL))
        assert mon.get_realized_pnl("BTC-PERP") == 0.0

    def test_increase_long_updates_avg_entry(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(price=52000, size=0.1, side=OrderSide.BUY, fill_id="f2"))
        # avg_entry = (50000*0.1 + 52000*0.1) / 0.2 = 51000
        assert mon._avg_entry["BTC-PERP"] == pytest.approx(51000.0)
        assert mon._position_size["BTC-PERP"] == pytest.approx(0.2)
        assert mon.get_realized_pnl("BTC-PERP") == 0.0

    def test_close_long_realizes_profit(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(price=51000, size=0.1, side=OrderSide.SELL, fill_id="f2"))
        # PnL = (51000 - 50000) * 0.1 = 100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)
        assert mon._position_size["BTC-PERP"] == pytest.approx(0.0)

    def test_close_long_realizes_loss(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(price=49000, size=0.1, side=OrderSide.SELL, fill_id="f2"))
        # PnL = (49000 - 50000) * 0.1 = -100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(-100.0)

    def test_close_short_realizes_profit(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.SELL, fill_id="f1"))
        mon.record_fill(_fill(price=49000, size=0.1, side=OrderSide.BUY, fill_id="f2"))
        # Short PnL = (50000 - 49000) * 0.1 = 100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)

    def test_close_short_realizes_loss(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.SELL, fill_id="f1"))
        mon.record_fill(_fill(price=51000, size=0.1, side=OrderSide.BUY, fill_id="f2"))
        # Short PnL = (50000 - 51000) * 0.1 = -100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(-100.0)

    def test_partial_close_keeps_avg_entry(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(price=50000, size=0.2, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(price=51000, size=0.1, side=OrderSide.SELL, fill_id="f2"))
        # Realized = (51000 - 50000) * 0.1 = 100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)
        assert mon._position_size["BTC-PERP"] == pytest.approx(0.1)
        # avg_entry unchanged after partial close
        assert mon._avg_entry["BTC-PERP"] == pytest.approx(50000.0)

    def test_flip_long_to_short(self):
        mon = PnLMonitor(_cfg())
        # Long 0.1 at 50000
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        # Sell 0.3 at 51000: closes 0.1 long, opens 0.2 short
        mon.record_fill(_fill(price=51000, size=0.3, side=OrderSide.SELL, fill_id="f2"))
        # Realized on closing portion = (51000 - 50000) * 0.1 = 100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)
        assert mon._position_size["BTC-PERP"] == pytest.approx(-0.2)
        # New entry price = fill price for flipped direction
        assert mon._avg_entry["BTC-PERP"] == pytest.approx(51000.0)

    def test_flip_short_to_long(self):
        mon = PnLMonitor(_cfg())
        # Short 0.1 at 50000
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.SELL, fill_id="f1"))
        # Buy 0.3 at 49000: closes 0.1 short, opens 0.2 long
        mon.record_fill(_fill(price=49000, size=0.3, side=OrderSide.BUY, fill_id="f2"))
        # Realized on closing portion = (50000 - 49000) * 0.1 = 100
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)
        assert mon._position_size["BTC-PERP"] == pytest.approx(0.2)
        assert mon._avg_entry["BTC-PERP"] == pytest.approx(49000.0)

    def test_multiple_symbols_independent(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(symbol="BTC-PERP", price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(symbol="ETH-PERP", price=3000, size=1.0, side=OrderSide.BUY, fill_id="f2"))
        mon.record_fill(_fill(symbol="BTC-PERP", price=51000, size=0.1, side=OrderSide.SELL, fill_id="f3"))
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(100.0)
        assert mon.get_realized_pnl("ETH-PERP") == pytest.approx(0.0)

    def test_fills_appended_to_deque(self):
        mon = PnLMonitor(_cfg())
        mon.record_fill(_fill(fill_id="f1"))
        mon.record_fill(_fill(fill_id="f2"))
        assert len(mon._fills["BTC-PERP"]) == 2

    def test_multiple_round_trips_accumulate_pnl(self):
        mon = PnLMonitor(_cfg())
        # Round trip 1: buy at 50000, sell at 51000 -> +100
        mon.record_fill(_fill(price=50000, size=0.1, side=OrderSide.BUY, fill_id="f1"))
        mon.record_fill(_fill(price=51000, size=0.1, side=OrderSide.SELL, fill_id="f2"))
        # Round trip 2: sell at 52000, buy at 51500 -> +50
        mon.record_fill(_fill(price=52000, size=0.1, side=OrderSide.SELL, fill_id="f3"))
        mon.record_fill(_fill(price=51500, size=0.1, side=OrderSide.BUY, fill_id="f4"))
        assert mon.get_realized_pnl("BTC-PERP") == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# 5.2 Funding tracking
# ---------------------------------------------------------------------------

class TestFundingTracking:
    """record_funding_payment accumulates running total."""

    def test_single_payment(self):
        mon = PnLMonitor(_cfg())
        mon.record_funding_payment("BTC-PERP", 5.0)
        assert mon.get_total_funding("BTC-PERP") == pytest.approx(5.0)

    def test_accumulates(self):
        mon = PnLMonitor(_cfg())
        mon.record_funding_payment("BTC-PERP", 5.0)
        mon.record_funding_payment("BTC-PERP", -2.0)
        mon.record_funding_payment("BTC-PERP", 3.0)
        assert mon.get_total_funding("BTC-PERP") == pytest.approx(6.0)

    def test_independent_per_symbol(self):
        mon = PnLMonitor(_cfg())
        mon.record_funding_payment("BTC-PERP", 10.0)
        mon.record_funding_payment("ETH-PERP", -3.0)
        assert mon.get_total_funding("BTC-PERP") == pytest.approx(10.0)
        assert mon.get_total_funding("ETH-PERP") == pytest.approx(-3.0)

    def test_default_zero_for_unknown_symbol(self):
        mon = PnLMonitor(_cfg())
        assert mon.get_total_funding("DOGE-PERP") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 5.3 Exchange cross-check
# ---------------------------------------------------------------------------

class TestCrosscheck:
    """crosscheck detects divergence and respects rate limiting."""

    @pytest.mark.asyncio
    async def test_no_divergence_returns_false(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 5.0
        # local = 105, exchange = 110, diff = 5 < 50
        result = await mon.crosscheck("BTC-PERP", 110.0, now_ms=100_000)
        assert result is False
        assert mon.is_diverged("BTC-PERP") is False

    @pytest.mark.asyncio
    async def test_divergence_detected(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 5.0
        # local = 105, exchange = 200, diff = 95 > 50
        result = await mon.crosscheck("BTC-PERP", 200.0, now_ms=100_000)
        assert result is True
        assert mon.is_diverged("BTC-PERP") is True

    @pytest.mark.asyncio
    async def test_divergence_clears_when_back_in_range(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0, crosscheck_interval=1.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 0.0
        # First check: divergence
        await mon.crosscheck("BTC-PERP", 200.0, now_ms=0)
        assert mon.is_diverged("BTC-PERP") is True
        # Second check after interval: no divergence
        result = await mon.crosscheck("BTC-PERP", 105.0, now_ms=2000)
        assert result is False
        assert mon.is_diverged("BTC-PERP") is False

    @pytest.mark.asyncio
    async def test_rate_limited_returns_cached_state(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0, crosscheck_interval=60.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 0.0
        # First check passes, no divergence
        await mon.crosscheck("BTC-PERP", 105.0, now_ms=0)
        # Second check within interval, should return cached False even though exchange_pnl would diverge
        result = await mon.crosscheck("BTC-PERP", 999.0, now_ms=30_000)
        assert result is False

    @pytest.mark.asyncio
    async def test_rate_limited_returns_cached_diverged_state(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0, crosscheck_interval=60.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 0.0
        # First check, diverged
        await mon.crosscheck("BTC-PERP", 999.0, now_ms=0)
        assert mon.is_diverged("BTC-PERP") is True
        # Within interval, returns cached True
        result = await mon.crosscheck("BTC-PERP", 100.0, now_ms=30_000)
        assert result is True

    @pytest.mark.asyncio
    async def test_runs_after_interval_elapsed(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0, crosscheck_interval=60.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 0.0
        # First check
        await mon.crosscheck("BTC-PERP", 105.0, now_ms=0)
        # After interval, should actually run
        result = await mon.crosscheck("BTC-PERP", 999.0, now_ms=60_001)
        assert result is True
        assert mon.is_diverged("BTC-PERP") is True

    @pytest.mark.asyncio
    async def test_exact_threshold_not_diverged(self):
        mon = PnLMonitor(_cfg(divergence_threshold=50.0))
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 0.0
        # diff = exactly 50, not > 50
        result = await mon.crosscheck("BTC-PERP", 150.0, now_ms=100_000)
        assert result is False


# ---------------------------------------------------------------------------
# 5.4 Total PnL computation
# ---------------------------------------------------------------------------

class TestComputeTotalPnl:
    """compute_total_pnl sums all components."""

    def test_all_components(self):
        mon = PnLMonitor(_cfg())
        mon._realized_pnl["BTC-PERP"] = 200.0
        mon._funding_payments["BTC-PERP"] = -15.0
        pos = _pos(unrealized_pnl=50.0)
        # 200 + 50 + (-15) = 235
        assert mon.compute_total_pnl("BTC-PERP", pos) == pytest.approx(235.0)

    def test_no_position(self):
        mon = PnLMonitor(_cfg())
        mon._realized_pnl["BTC-PERP"] = 100.0
        mon._funding_payments["BTC-PERP"] = 10.0
        # realized + 0 + funding = 110
        assert mon.compute_total_pnl("BTC-PERP", None) == pytest.approx(110.0)

    def test_no_data_returns_zero(self):
        mon = PnLMonitor(_cfg())
        assert mon.compute_total_pnl("BTC-PERP", None) == pytest.approx(0.0)

    def test_negative_unrealized(self):
        mon = PnLMonitor(_cfg())
        mon._realized_pnl["BTC-PERP"] = 50.0
        mon._funding_payments["BTC-PERP"] = 0.0
        pos = _pos(unrealized_pnl=-200.0)
        assert mon.compute_total_pnl("BTC-PERP", pos) == pytest.approx(-150.0)
