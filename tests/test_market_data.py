"""Tests for MarketData — vol metrics, price handling, order updates, REST fetches."""

import asyncio
import math
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from gridbot.config import AssetConfig, BotConfig
from gridbot.market_data import (
    MarketData,
    _DEFAULT_REALIZED_VOL,
    _DEFAULT_ROLLING_RETURN,
    _DEFAULT_SPREAD_BPS,
    _L2_STALENESS_MS,
    _MAKER_FEE_RATE,
    _MIN_CANDLES_FOR_ATR,
    _MIN_TRADES_FOR_VOL,
    _SECONDS_PER_YEAR,
    _TAKER_FEE_RATE,
    _VOL_SAMPLE_INTERVAL_S,
)
from gridbot.types import Fill, OpenOrder, OrderSide, Position, VolMetrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _config(**overrides) -> BotConfig:
    """Minimal BotConfig for testing."""
    cfg = BotConfig(
        testnet=True,
        wallet_address="0x" + "ab" * 20,
        assets=[AssetConfig(symbol="BTC-PERP")],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _two_asset_config() -> BotConfig:
    return BotConfig(
        testnet=True,
        wallet_address="0x" + "ab" * 20,
        assets=[AssetConfig(symbol="BTC-PERP"), AssetConfig(symbol="ETH-PERP")],
    )


@pytest.fixture
def md() -> MarketData:
    """MarketData instance with BTC-PERP only (no live connection)."""
    return MarketData(_config())


@pytest.fixture
def md2() -> MarketData:
    """MarketData instance with BTC-PERP and ETH-PERP."""
    return MarketData(_two_asset_config())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _populate_trades(md: MarketData, symbol: str, prices: list[float],
                     start_ms: int = 1_000_000, interval_ms: int = 5_000) -> None:
    """Fill the return buffer with synthetic trades at fixed intervals."""
    for i, px in enumerate(prices):
        ts = start_ms + i * interval_ms
        md._return_buffers[symbol].append((ts, px))


def _populate_candles(md: MarketData, symbol: str, candles: list[dict]) -> None:
    """Fill the minute candle buffer directly."""
    for c in candles:
        md._minute_candles[symbol].append(c)


def _make_candle(open_: float, high: float, low: float, close: float,
                 ts: int = 0, volume: float = 1.0) -> dict:
    return {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "timestamp_ms": ts,
    }


# ===========================================================================
# 1. Coin/symbol mapping
# ===========================================================================

class TestCoinSymbolMapping:
    def test_to_coin(self, md: MarketData):
        assert md._to_coin("BTC-PERP") == "BTC"

    def test_to_symbol(self, md: MarketData):
        assert md._to_symbol("BTC") == "BTC-PERP"

    def test_to_symbol_unknown_coin(self, md: MarketData):
        assert md._to_symbol("DOGE") == ""

    def test_two_asset_mapping(self, md2: MarketData):
        assert md2._to_coin("ETH-PERP") == "ETH"
        assert md2._to_symbol("ETH") == "ETH-PERP"
        assert md2._to_symbol("BTC") == "BTC-PERP"

    def test_case_insensitive_lookup(self, md: MarketData):
        assert md._to_symbol("btc") == "BTC-PERP"
        assert md._to_symbol("Btc") == "BTC-PERP"


# ===========================================================================
# 2. Price update handling (_handle_price_update)
# ===========================================================================

class TestPriceUpdate:
    @pytest.mark.asyncio
    async def test_stores_bid_ask_mid(self, md: MarketData):
        await md._handle_price_update("BTC-PERP", 60000.0, 60010.0)
        assert md._best_bid["BTC-PERP"] == 60000.0
        assert md._best_ask["BTC-PERP"] == 60010.0
        assert md._mid_prices["BTC-PERP"] == pytest.approx(60005.0)

    @pytest.mark.asyncio
    async def test_mid_is_average(self, md: MarketData):
        await md._handle_price_update("BTC-PERP", 100.0, 200.0)
        assert md._mid_prices["BTC-PERP"] == pytest.approx(150.0)

    @pytest.mark.asyncio
    async def test_get_mid_price(self, md: MarketData):
        assert md.get_mid_price("BTC-PERP") == 0.0
        await md._handle_price_update("BTC-PERP", 50000.0, 50010.0)
        assert md.get_mid_price("BTC-PERP") == pytest.approx(50005.0)

    @pytest.mark.asyncio
    async def test_overwrite_on_update(self, md: MarketData):
        await md._handle_price_update("BTC-PERP", 60000.0, 60010.0)
        await md._handle_price_update("BTC-PERP", 61000.0, 61020.0)
        assert md._best_bid["BTC-PERP"] == 61000.0
        assert md._mid_prices["BTC-PERP"] == pytest.approx(61010.0)


# ===========================================================================
# 3. Trade handling (_handle_trade)
# ===========================================================================

class TestTradeHandling:
    @pytest.mark.asyncio
    async def test_appends_to_return_buffer(self, md: MarketData):
        await md._handle_trade("BTC-PERP", 60000.0, 0.1, 1_000_000)
        await md._handle_trade("BTC-PERP", 60010.0, 0.2, 1_005_000)
        buf = md._return_buffers["BTC-PERP"]
        assert len(buf) == 2
        assert buf[0] == (1_000_000, 60000.0)
        assert buf[1] == (1_005_000, 60010.0)

    @pytest.mark.asyncio
    async def test_creates_candle(self, md: MarketData):
        ts = 60_000  # minute 1
        await md._handle_trade("BTC-PERP", 100.0, 1.0, ts)
        await md._handle_trade("BTC-PERP", 110.0, 0.5, ts + 10_000)
        await md._handle_trade("BTC-PERP", 95.0, 0.3, ts + 20_000)

        candle = md._current_candle["BTC-PERP"]
        assert candle["open"] == 100.0
        assert candle["high"] == 110.0
        assert candle["low"] == 95.0
        assert candle["close"] == 95.0
        assert candle["volume"] == pytest.approx(1.8)

    @pytest.mark.asyncio
    async def test_new_minute_finalizes_candle(self, md: MarketData):
        # Minute 1
        await md._handle_trade("BTC-PERP", 100.0, 1.0, 60_000)
        await md._handle_trade("BTC-PERP", 110.0, 1.0, 90_000)
        # Minute 2 — should finalize minute 1
        await md._handle_trade("BTC-PERP", 105.0, 1.0, 120_000)

        assert len(md._minute_candles["BTC-PERP"]) == 1
        finalized = md._minute_candles["BTC-PERP"][0]
        assert finalized["open"] == 100.0
        assert finalized["high"] == 110.0
        assert finalized["close"] == 110.0
        assert finalized["timestamp_ms"] == 60_000

        current = md._current_candle["BTC-PERP"]
        assert current["open"] == 105.0
        assert current["timestamp_ms"] == 120_000

    @pytest.mark.asyncio
    async def test_multiple_candle_rollovers(self, md: MarketData):
        for minute in range(5):
            ts = (minute + 1) * 60_000
            await md._handle_trade("BTC-PERP", 100.0 + minute, 1.0, ts)

        # 4 finalized candles (minutes 1-4), minute 5 is current
        assert len(md._minute_candles["BTC-PERP"]) == 4


# ===========================================================================
# 4. Spread computation
# ===========================================================================

class TestSpreadComputation:
    def test_normal_spread(self, md: MarketData):
        md._mid_prices["BTC-PERP"] = 60000.0
        md._best_bid["BTC-PERP"] = 59997.0
        md._best_ask["BTC-PERP"] = 60003.0
        vm = md.compute_vol_metrics("BTC-PERP")
        expected = (60003.0 - 59997.0) / 60000.0 * 10_000
        assert vm.spread_bps == pytest.approx(expected)

    def test_zero_spread(self, md: MarketData):
        md._mid_prices["BTC-PERP"] = 60000.0
        md._best_bid["BTC-PERP"] = 60000.0
        md._best_ask["BTC-PERP"] = 60000.0
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.spread_bps == pytest.approx(0.0)

    def test_no_price_data_returns_default(self, md: MarketData):
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.spread_bps == _DEFAULT_SPREAD_BPS

    def test_missing_bid_returns_default(self, md: MarketData):
        md._mid_prices["BTC-PERP"] = 60000.0
        md._best_ask["BTC-PERP"] = 60003.0
        # bid missing → default
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.spread_bps == _DEFAULT_SPREAD_BPS


# ===========================================================================
# 5. Realized vol computation
# ===========================================================================

class TestRealizedVol:
    def test_insufficient_data_returns_default(self, md: MarketData):
        _populate_trades(md, "BTC-PERP", [100.0] * (_MIN_TRADES_FOR_VOL - 1))
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.realized_vol == _DEFAULT_REALIZED_VOL

    def test_constant_price_zero_vol(self, md: MarketData):
        """Constant prices → zero realized vol."""
        prices = [50000.0] * 100
        _populate_trades(md, "BTC-PERP", prices, interval_ms=5_000)
        md._mid_prices["BTC-PERP"] = 50000.0
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.realized_vol == pytest.approx(0.0, abs=1e-10)

    def test_known_vol(self, md: MarketData):
        """Generate prices with known log-return std dev, verify annualization."""
        import random
        random.seed(42)

        base = 50000.0
        n = 200
        daily_vol = 0.01  # 1% per 5s sample
        interval_ms = _VOL_SAMPLE_INTERVAL_S * 1000

        prices = [base]
        for _ in range(n - 1):
            ret = random.gauss(0, daily_vol)
            prices.append(prices[-1] * math.exp(ret))

        _populate_trades(md, "BTC-PERP", prices, interval_ms=interval_ms)
        md._mid_prices["BTC-PERP"] = prices[-1]

        vm = md.compute_vol_metrics("BTC-PERP")

        # Expected annualized vol: daily_vol * sqrt(periods_per_year)
        periods_per_year = _SECONDS_PER_YEAR / _VOL_SAMPLE_INTERVAL_S
        expected_annual = daily_vol * math.sqrt(periods_per_year)

        # Allow 20% tolerance due to sampling noise
        assert vm.realized_vol == pytest.approx(expected_annual, rel=0.20)

    def test_vol_increases_with_larger_moves(self, md: MarketData):
        """Higher volatility prices produce higher realized vol."""
        # Low vol: small oscillation
        low_prices = [50000.0 + (i % 2) * 5 for i in range(60)]
        _populate_trades(md, "BTC-PERP", low_prices, interval_ms=5_000)
        md._mid_prices["BTC-PERP"] = low_prices[-1]
        low_vol = md.compute_vol_metrics("BTC-PERP").realized_vol

        # High vol: large oscillation
        md2 = MarketData(_config())
        high_prices = [50000.0 + (i % 2) * 500 for i in range(60)]
        _populate_trades(md2, "BTC-PERP", high_prices, interval_ms=5_000)
        md2._mid_prices["BTC-PERP"] = high_prices[-1]
        high_vol = md2.compute_vol_metrics("BTC-PERP").realized_vol

        assert high_vol > low_vol

    def test_too_short_time_span_returns_default(self, md: MarketData):
        """All trades within one sample interval → insufficient for vol."""
        _populate_trades(md, "BTC-PERP", [50000.0] * 50, interval_ms=1)
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.realized_vol == _DEFAULT_REALIZED_VOL


# ===========================================================================
# 6. ATR computation
# ===========================================================================

class TestATR:
    def test_insufficient_candles_returns_default(self, md: MarketData):
        md._mid_prices["BTC-PERP"] = 50000.0
        candles = [_make_candle(100, 110, 90, 100, ts=i * 60_000) for i in range(_MIN_CANDLES_FOR_ATR - 1)]
        _populate_candles(md, "BTC-PERP", candles)
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.atr == pytest.approx(50000.0 * 0.02)

    def test_known_atr(self, md: MarketData):
        """14 candles with known high-low range → predictable ATR."""
        md._mid_prices["BTC-PERP"] = 100.0
        candles = [_make_candle(100, 120, 80, 100, ts=i * 60_000) for i in range(14)]
        _populate_candles(md, "BTC-PERP", candles)
        vm = md.compute_vol_metrics("BTC-PERP")
        # First candle: TR = 120 - 80 = 40
        # Remaining candles: TR = max(40, |120-100|, |80-100|) = 40
        assert vm.atr == pytest.approx(40.0)

    def test_atr_with_gaps(self, md: MarketData):
        """ATR uses true range (accounts for gap from previous close)."""
        md._mid_prices["BTC-PERP"] = 100.0
        candles = []
        # First candle: close at 100
        candles.append(_make_candle(100, 105, 95, 100, ts=0))
        # Remaining candles gap up: open 110, high 115, low 108, close 112
        for i in range(1, _MIN_CANDLES_FOR_ATR):
            candles.append(_make_candle(110, 115, 108, 112, ts=i * 60_000))

        _populate_candles(md, "BTC-PERP", candles)
        vm = md.compute_vol_metrics("BTC-PERP")

        # Candle 0: TR = 105 - 95 = 10
        # Candle 1: TR = max(115-108, |115-100|, |108-100|) = max(7, 15, 8) = 15
        # Candles 2-13: TR = max(7, |115-112|, |108-112|) = max(7, 3, 4) = 7
        expected_atr = (10 + 15 + 7 * 12) / 14
        assert vm.atr == pytest.approx(expected_atr)

    def test_atr_no_mid_price_returns_zero(self, md: MarketData):
        """No mid price and insufficient candles → ATR = 0."""
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.atr == 0.0

    def test_atr_uses_last_n_candles(self, md: MarketData):
        """With more than N candles, only the last N are used."""
        md._mid_prices["BTC-PERP"] = 100.0
        # 20 candles: first 6 have wide range, last 14 have narrow range
        candles = [_make_candle(100, 200, 50, 100, ts=i * 60_000) for i in range(6)]
        candles += [_make_candle(100, 102, 98, 100, ts=(i + 6) * 60_000) for i in range(14)]
        _populate_candles(md, "BTC-PERP", candles)
        vm = md.compute_vol_metrics("BTC-PERP")
        # Last 14 candles have H-L = 4, prev close = 100
        # Candle index 6 (first of the narrow ones): TR = max(4, |102-100|, |98-100|) = 4
        assert vm.atr == pytest.approx(4.0)


# ===========================================================================
# 7. Rolling returns
# ===========================================================================

class TestRollingReturns:
    def test_insufficient_data_returns_default(self, md: MarketData):
        _populate_trades(md, "BTC-PERP", [100.0])
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.rolling_return_1m == _DEFAULT_ROLLING_RETURN
        assert vm.rolling_return_5m == _DEFAULT_ROLLING_RETURN

    def test_1m_return(self, md: MarketData):
        """Known 1m return from price change."""
        md._mid_prices["BTC-PERP"] = 105.0
        # Price at t=0: 100, price at t=60s: 105
        md._return_buffers["BTC-PERP"].append((0, 100.0))
        md._return_buffers["BTC-PERP"].append((60_000, 105.0))
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.rolling_return_1m == pytest.approx(0.05)  # 5%

    def test_5m_return(self, md: MarketData):
        """Known 5m return from price change."""
        md._mid_prices["BTC-PERP"] = 110.0
        md._return_buffers["BTC-PERP"].append((0, 100.0))
        md._return_buffers["BTC-PERP"].append((300_000, 110.0))
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.rolling_return_5m == pytest.approx(0.10)  # 10%

    def test_negative_return(self, md: MarketData):
        md._mid_prices["BTC-PERP"] = 90.0
        md._return_buffers["BTC-PERP"].append((0, 100.0))
        md._return_buffers["BTC-PERP"].append((60_000, 90.0))
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.rolling_return_1m == pytest.approx(-0.10)

    def test_uses_closest_past_price(self, md: MarketData):
        """Multiple trades before the window; use the one closest to the boundary."""
        md._mid_prices["BTC-PERP"] = 110.0
        # Trades at 0s, 30s, 55s, 60s
        md._return_buffers["BTC-PERP"].append((0, 90.0))
        md._return_buffers["BTC-PERP"].append((30_000, 95.0))
        md._return_buffers["BTC-PERP"].append((55_000, 100.0))
        md._return_buffers["BTC-PERP"].append((60_000, 110.0))
        vm = md.compute_vol_metrics("BTC-PERP")
        # 1m window: target = 60000 - 60000 = 0. Price at t<=0 is 90.
        assert vm.rolling_return_1m == pytest.approx((110 - 90) / 90)


# ===========================================================================
# 8. compute_vol_metrics full integration
# ===========================================================================

class TestVolMetricsIntegration:
    def test_all_defaults_no_data(self, md: MarketData):
        vm = md.compute_vol_metrics("BTC-PERP")
        assert vm.realized_vol == _DEFAULT_REALIZED_VOL
        assert vm.spread_bps == _DEFAULT_SPREAD_BPS
        assert vm.rolling_return_1m == _DEFAULT_ROLLING_RETURN
        assert vm.rolling_return_5m == _DEFAULT_ROLLING_RETURN
        assert vm.atr == 0.0

    def test_returns_vol_metrics_type(self, md: MarketData):
        vm = md.compute_vol_metrics("BTC-PERP")
        assert isinstance(vm, VolMetrics)

    def test_unknown_symbol_returns_defaults(self, md: MarketData):
        vm = md.compute_vol_metrics("DOGE-PERP")
        assert vm.realized_vol == _DEFAULT_REALIZED_VOL


# ===========================================================================
# 9. Order update handling (_handle_order_update)
# ===========================================================================

class TestOrderUpdateHandling:
    def _make_update(self, oid: int = 1, coin: str = "BTC", side: str = "B",
                     limit_px: str = "60000.0", sz: str = "1.0",
                     orig_sz: str = "1.0", status: str = "open",
                     status_ts: int = 100_000, cloid: str = "") -> dict:
        return {
            "order": {
                "oid": oid,
                "coin": coin,
                "side": side,
                "limitPx": limit_px,
                "sz": sz,
                "origSz": orig_sz,
                "cloid": cloid,
            },
            "status": status,
            "statusTimestamp": status_ts,
        }

    @pytest.mark.asyncio
    async def test_new_order_starts_tracking(self, md: MarketData):
        update = self._make_update(oid=10, status="open", sz="1.0", orig_sz="1.0")
        result = await md._handle_order_update(update)
        assert result is None
        assert 10 in md._tracked_orders
        assert md._tracked_orders[10]["remaining_sz"] == 1.0

    @pytest.mark.asyncio
    async def test_full_fill_returns_fill(self, md: MarketData):
        # First track the order
        await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="1.0", orig_sz="1.0")
        )
        # Then fill it
        fill = await md._handle_order_update(
            self._make_update(oid=10, status="filled", sz="0.0", orig_sz="1.0",
                              status_ts=200_000)
        )
        assert fill is not None
        assert isinstance(fill, Fill)
        assert fill.order_id == 10
        assert fill.symbol == "BTC-PERP"
        assert fill.price == 60000.0
        assert fill.size == 1.0
        assert fill.side == OrderSide.BUY
        assert fill.is_maker is True
        assert fill.is_partial is False
        assert fill.timestamp_ms == 200_000
        # Should be removed from tracking
        assert 10 not in md._tracked_orders

    @pytest.mark.asyncio
    async def test_fill_fee_calculation(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="2.0", orig_sz="2.0",
                              limit_px="50000.0")
        )
        fill = await md._handle_order_update(
            self._make_update(oid=10, status="filled", sz="0.0", orig_sz="2.0",
                              limit_px="50000.0")
        )
        expected_fee = 2.0 * 50000.0 * _MAKER_FEE_RATE
        assert fill.fee == pytest.approx(expected_fee)

    @pytest.mark.asyncio
    async def test_partial_fill_returns_fill_with_flag(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="1.0", orig_sz="1.0")
        )
        # Partial: remaining drops from 1.0 to 0.5
        result = await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="0.5", orig_sz="1.0")
        )
        assert result is not None
        assert isinstance(result, Fill)
        assert result.size == pytest.approx(0.5)
        assert result.is_partial is True
        assert md._tracked_orders[10]["remaining_sz"] == 0.5

    @pytest.mark.asyncio
    async def test_cancel_removes_tracking(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=10, status="open")
        )
        result = await md._handle_order_update(
            self._make_update(oid=10, status="canceled")
        )
        assert result is None
        assert 10 not in md._tracked_orders

    @pytest.mark.asyncio
    async def test_margin_cancel_removes_tracking(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=10, status="open")
        )
        result = await md._handle_order_update(
            self._make_update(oid=10, status="marginCanceled")
        )
        assert result is None
        assert 10 not in md._tracked_orders

    @pytest.mark.asyncio
    async def test_rejected_removes_tracking(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=10, status="open")
        )
        await md._handle_order_update(
            self._make_update(oid=10, status="rejected")
        )
        assert 10 not in md._tracked_orders

    @pytest.mark.asyncio
    async def test_fill_without_prior_tracking(self, md: MarketData):
        """Fill arrives without a prior 'open' — uses origSz."""
        fill = await md._handle_order_update(
            self._make_update(oid=99, status="filled", sz="0.0", orig_sz="3.0",
                              limit_px="40000.0", status_ts=500_000)
        )
        assert fill is not None
        assert fill.size == 3.0
        assert fill.price == 40000.0

    @pytest.mark.asyncio
    async def test_sell_side_mapping(self, md: MarketData):
        fill = await md._handle_order_update(
            self._make_update(oid=20, status="filled", side="A", orig_sz="1.0")
        )
        assert fill.side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_unknown_coin_returns_none(self, md: MarketData):
        update = self._make_update(coin="DOGE")
        result = await md._handle_order_update(update)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_oid_returns_none(self, md: MarketData):
        result = await md._handle_order_update({"order": {}, "status": "open"})
        assert result is None

    @pytest.mark.asyncio
    async def test_fill_id_format(self, md: MarketData):
        fill = await md._handle_order_update(
            self._make_update(oid=42, status="filled", status_ts=123_456)
        )
        assert fill.fill_id == "42-123456"

    @pytest.mark.asyncio
    async def test_cloid_propagated(self, md: MarketData):
        fill = await md._handle_order_update(
            self._make_update(oid=10, status="filled", cloid="my-order-1")
        )
        assert fill.client_order_id == "my-order-1"

    @pytest.mark.asyncio
    async def test_partial_then_full_fill(self, md: MarketData):
        """Partial emits is_partial=True, full fill emits remaining size."""
        await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="2.0", orig_sz="2.0")
        )
        # Partial: 2.0 → 0.8 (filled 1.2)
        partial = await md._handle_order_update(
            self._make_update(oid=10, status="open", sz="0.8", orig_sz="2.0")
        )
        assert partial is not None
        assert partial.size == pytest.approx(1.2)
        assert partial.is_partial is True

        # Full fill of remaining 0.8
        fill = await md._handle_order_update(
            self._make_update(oid=10, status="filled", sz="0.0", orig_sz="2.0")
        )
        assert fill.size == pytest.approx(0.8)
        assert fill.is_partial is False

    @pytest.mark.asyncio
    async def test_ioc_order_detected_as_taker(self, md: MarketData):
        """IOC time-in-force → is_maker=False and taker fee rate."""
        update = self._make_update(oid=30, status="filled", orig_sz="1.0",
                                   limit_px="50000.0")
        update["order"]["tif"] = "Ioc"
        fill = await md._handle_order_update(update)
        assert fill.is_maker is False
        expected_fee = 1.0 * 50000.0 * _TAKER_FEE_RATE
        assert fill.fee == pytest.approx(expected_fee)

    @pytest.mark.asyncio
    async def test_alo_order_detected_as_maker(self, md: MarketData):
        """ALO time-in-force → is_maker=True and maker fee rate."""
        update = self._make_update(oid=31, status="filled", orig_sz="2.0",
                                   limit_px="50000.0")
        update["order"]["tif"] = "Alo"
        fill = await md._handle_order_update(update)
        assert fill.is_maker is True
        expected_fee = 2.0 * 50000.0 * _MAKER_FEE_RATE
        assert fill.fee == pytest.approx(expected_fee)

    @pytest.mark.asyncio
    async def test_no_tif_defaults_to_maker(self, md: MarketData):
        """Missing tif field defaults to maker (grid orders are ALO)."""
        fill = await md._handle_order_update(
            self._make_update(oid=32, status="filled", orig_sz="1.0")
        )
        assert fill.is_maker is True

    @pytest.mark.asyncio
    async def test_taker_tif_propagated_from_tracked(self, md: MarketData):
        """Taker status recorded at open is preserved through to full fill."""
        open_update = self._make_update(oid=33, status="open", sz="1.0", orig_sz="1.0")
        open_update["order"]["tif"] = "Ioc"
        await md._handle_order_update(open_update)

        fill = await md._handle_order_update(
            self._make_update(oid=33, status="filled", sz="0.0", orig_sz="1.0",
                              limit_px="50000.0")
        )
        assert fill.is_maker is False
        assert fill.fee == pytest.approx(1.0 * 50000.0 * _TAKER_FEE_RATE)

    @pytest.mark.asyncio
    async def test_partial_fill_id_format(self, md: MarketData):
        await md._handle_order_update(
            self._make_update(oid=40, status="open", sz="2.0", orig_sz="2.0")
        )
        fill = await md._handle_order_update(
            self._make_update(oid=40, status="open", sz="1.0", orig_sz="2.0",
                              status_ts=555_000)
        )
        assert fill.fill_id == "40-partial-555000"

    @pytest.mark.asyncio
    async def test_no_size_change_no_partial_fill(self, md: MarketData):
        """Open update with unchanged remaining → no fill emitted."""
        await md._handle_order_update(
            self._make_update(oid=50, status="open", sz="1.0", orig_sz="1.0")
        )
        result = await md._handle_order_update(
            self._make_update(oid=50, status="open", sz="1.0", orig_sz="1.0")
        )
        assert result is None


# ===========================================================================
# 10. allMids fallback
# ===========================================================================

class TestAllMidsFallback:
    @pytest.mark.asyncio
    async def test_uses_allmids_when_l2_stale(self, md: MarketData):
        """allMids updates mid price when l2Book is stale."""
        md._last_l2_update_ms["BTC-PERP"] = 0  # Never updated
        msg = {"data": {"mids": {"BTC": "55000.0"}}}
        await md._dispatch_all_mids(msg)
        assert md._mid_prices["BTC-PERP"] == pytest.approx(55000.0)

    @pytest.mark.asyncio
    async def test_ignores_allmids_when_l2_fresh(self, md: MarketData):
        """allMids does not overwrite when l2Book is fresh."""
        import time
        md._last_l2_update_ms["BTC-PERP"] = int(time.time() * 1000)
        md._mid_prices["BTC-PERP"] = 60000.0

        msg = {"data": {"mids": {"BTC": "55000.0"}}}
        await md._dispatch_all_mids(msg)
        assert md._mid_prices["BTC-PERP"] == pytest.approx(60000.0)

    @pytest.mark.asyncio
    async def test_unknown_coin_ignored(self, md: MarketData):
        msg = {"data": {"mids": {"DOGE": "0.1"}}}
        await md._dispatch_all_mids(msg)
        assert "DOGE-PERP" not in md._mid_prices


# ===========================================================================
# 11. Mark price via activeAssetCtx
# ===========================================================================

class TestMarkPrice:
    @pytest.mark.asyncio
    async def test_stores_mark_price(self, md: MarketData):
        msg = {"data": {"coin": "BTC", "ctx": {"markPx": "61234.5"}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_mark_price("BTC-PERP") == pytest.approx(61234.5)

    @pytest.mark.asyncio
    async def test_unknown_coin_ignored(self, md: MarketData):
        msg = {"data": {"coin": "DOGE", "ctx": {"markPx": "0.1"}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_mark_price("DOGE-PERP") == 0.0

    @pytest.mark.asyncio
    async def test_missing_mark_px_ignored(self, md: MarketData):
        md._mark_prices["BTC-PERP"] = 60000.0
        msg = {"data": {"coin": "BTC", "ctx": {}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_mark_price("BTC-PERP") == 60000.0

    @pytest.mark.asyncio
    async def test_stores_funding_rate(self, md: MarketData):
        msg = {"data": {"coin": "BTC", "ctx": {"markPx": "61000.0", "funding": "0.0003"}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_funding_rate("BTC-PERP") == pytest.approx(0.0003)

    @pytest.mark.asyncio
    async def test_funding_rate_default_zero(self, md: MarketData):
        assert md.get_funding_rate("BTC-PERP") == 0.0

    @pytest.mark.asyncio
    async def test_missing_funding_preserves_old(self, md: MarketData):
        md._funding_rates["BTC-PERP"] = 0.001
        msg = {"data": {"coin": "BTC", "ctx": {"markPx": "61000.0"}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_funding_rate("BTC-PERP") == pytest.approx(0.001)

    @pytest.mark.asyncio
    async def test_funding_without_mark_px(self, md: MarketData):
        msg = {"data": {"coin": "BTC", "ctx": {"funding": "0.0005"}}}
        await md._dispatch_asset_ctx(msg)
        assert md.get_funding_rate("BTC-PERP") == pytest.approx(0.0005)
        assert md.get_mark_price("BTC-PERP") == 0.0


# ===========================================================================
# 12. l2Book dispatch
# ===========================================================================

class TestL2BookDispatch:
    @pytest.mark.asyncio
    async def test_updates_price_from_l2(self, md: MarketData):
        msg = {
            "data": {
                "coin": "BTC",
                "levels": [
                    [{"px": "60000.0", "sz": "1.0", "n": 1}],
                    [{"px": "60010.0", "sz": "1.0", "n": 1}],
                ],
                "time": 123456,
            }
        }
        await md._dispatch_l2_book(msg)
        assert md._best_bid["BTC-PERP"] == 60000.0
        assert md._best_ask["BTC-PERP"] == 60010.0
        assert md._mid_prices["BTC-PERP"] == pytest.approx(60005.0)

    @pytest.mark.asyncio
    async def test_empty_levels_ignored(self, md: MarketData):
        msg = {"data": {"coin": "BTC", "levels": [[], []], "time": 0}}
        await md._dispatch_l2_book(msg)
        assert "BTC-PERP" not in md._mid_prices

    @pytest.mark.asyncio
    async def test_unknown_coin_ignored(self, md: MarketData):
        msg = {
            "data": {
                "coin": "DOGE",
                "levels": [[{"px": "0.1", "sz": "1", "n": 1}],
                           [{"px": "0.2", "sz": "1", "n": 1}]],
                "time": 0,
            }
        }
        await md._dispatch_l2_book(msg)
        assert "DOGE-PERP" not in md._mid_prices


# ===========================================================================
# 13. Trades dispatch
# ===========================================================================

class TestTradesDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_trades_to_buffer(self, md: MarketData):
        msg = {
            "data": [
                {"coin": "BTC", "px": "60000.0", "sz": "0.5", "time": 1_000_000,
                 "side": "B", "hash": "0x1"},
                {"coin": "BTC", "px": "60001.0", "sz": "0.3", "time": 1_001_000,
                 "side": "A", "hash": "0x2"},
            ]
        }
        await md._dispatch_trades(msg)
        assert len(md._return_buffers["BTC-PERP"]) == 2

    @pytest.mark.asyncio
    async def test_empty_trades_no_op(self, md: MarketData):
        await md._dispatch_trades({"data": []})
        assert len(md._return_buffers["BTC-PERP"]) == 0


# ===========================================================================
# 14. Order updates dispatch
# ===========================================================================

class TestOrderUpdatesDispatch:
    @pytest.mark.asyncio
    async def test_fill_enqueued(self, md: MarketData):
        md.fills = asyncio.Queue()
        msg = {
            "data": [
                {
                    "order": {
                        "oid": 1, "coin": "BTC", "side": "B",
                        "limitPx": "60000.0", "sz": "0.0",
                        "origSz": "1.0", "cloid": "",
                    },
                    "status": "filled",
                    "statusTimestamp": 100_000,
                }
            ]
        }
        await md._dispatch_order_updates(msg)
        assert not md.fills.empty()
        fill = await md.fills.get()
        assert isinstance(fill, Fill)

    @pytest.mark.asyncio
    async def test_new_open_order_not_enqueued(self, md: MarketData):
        """First 'open' status (no prior tracking) does not produce a fill."""
        md.fills = asyncio.Queue()
        msg = {
            "data": [
                {
                    "order": {
                        "oid": 1, "coin": "BTC", "side": "B",
                        "limitPx": "60000.0", "sz": "1.0",
                        "origSz": "1.0", "cloid": "",
                    },
                    "status": "open",
                    "statusTimestamp": 100_000,
                }
            ]
        }
        await md._dispatch_order_updates(msg)
        assert md.fills.empty()

    @pytest.mark.asyncio
    async def test_partial_fill_enqueued(self, md: MarketData):
        """Partial fill (remaining decreases) enqueues a Fill with is_partial."""
        md.fills = asyncio.Queue()
        # First: new open order
        await md._dispatch_order_updates({"data": [{
            "order": {"oid": 1, "coin": "BTC", "side": "B",
                      "limitPx": "60000.0", "sz": "1.0",
                      "origSz": "1.0", "cloid": ""},
            "status": "open", "statusTimestamp": 100_000,
        }]})
        assert md.fills.empty()

        # Second: partial fill (remaining 1.0 → 0.4)
        await md._dispatch_order_updates({"data": [{
            "order": {"oid": 1, "coin": "BTC", "side": "B",
                      "limitPx": "60000.0", "sz": "0.4",
                      "origSz": "1.0", "cloid": ""},
            "status": "open", "statusTimestamp": 101_000,
        }]})
        assert not md.fills.empty()
        fill = await md.fills.get()
        assert fill.size == pytest.approx(0.6)
        assert fill.is_partial is True


# ===========================================================================
# 15. REST backup fetches (mocked SDK)
# ===========================================================================

class TestRESTFetches:
    @pytest_asyncio.fixture
    async def md_with_info(self, md: MarketData):
        """MarketData with a mocked Info client."""
        md._info = MagicMock()
        return md

    @pytest.mark.asyncio
    async def test_fetch_open_orders(self, md_with_info: MarketData):
        md = md_with_info
        md._info.frontend_open_orders = MagicMock(return_value=[
            {
                "oid": 100,
                "coin": "BTC",
                "side": "B",
                "limitPx": "59000.0",
                "sz": "0.5",
                "origSz": "1.0",
                "reduceOnly": False,
                "cloid": "",
            },
            {
                "oid": 101,
                "coin": "ETH",
                "side": "A",
                "limitPx": "3000.0",
                "sz": "2.0",
                "origSz": "2.0",
                "reduceOnly": True,
            },
        ])
        orders = await md.fetch_open_orders("BTC-PERP")
        assert len(orders) == 1
        o = orders[0]
        assert o.order_id == 100
        assert o.symbol == "BTC-PERP"
        assert o.price == 59000.0
        assert o.size == 1.0
        assert o.remaining == 0.5
        assert o.side == OrderSide.BUY
        assert o.reduce_only is False

    @pytest.mark.asyncio
    async def test_fetch_open_orders_filters_by_symbol(self, md_with_info: MarketData):
        md = md_with_info
        md._info.frontend_open_orders = MagicMock(return_value=[
            {"oid": 1, "coin": "ETH", "side": "B", "limitPx": "3000",
             "sz": "1", "origSz": "1"},
        ])
        orders = await md.fetch_open_orders("BTC-PERP")
        assert len(orders) == 0

    @pytest.mark.asyncio
    async def test_fetch_open_orders_no_info(self, md: MarketData):
        orders = await md.fetch_open_orders("BTC-PERP")
        assert orders == []

    @pytest.mark.asyncio
    async def test_fetch_position(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.5",
                        "entryPx": "58000.0",
                        "unrealizedPnl": "1000.0",
                        "liquidationPx": "40000.0",
                    },
                    "type": "oneWay",
                }
            ]
        })
        pos = await md.fetch_position("BTC-PERP")
        assert pos is not None
        assert pos.symbol == "BTC-PERP"
        assert pos.size == 0.5
        assert pos.avg_entry_price == 58000.0
        assert pos.unrealized_pnl == 1000.0
        assert pos.liquidation_price == 40000.0

    @pytest.mark.asyncio
    async def test_fetch_position_zero_size(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0"}, "type": "oneWay"}
            ]
        })
        pos = await md.fetch_position("BTC-PERP")
        assert pos is None

    @pytest.mark.asyncio
    async def test_fetch_position_no_match(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={
            "assetPositions": [
                {"position": {"coin": "ETH", "szi": "1.0"}, "type": "oneWay"}
            ]
        })
        pos = await md.fetch_position("BTC-PERP")
        assert pos is None

    @pytest.mark.asyncio
    async def test_fetch_position_no_liq_price(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC", "szi": "0.5", "entryPx": "58000.0",
                        "unrealizedPnl": "0", "liquidationPx": None,
                    },
                    "type": "oneWay",
                }
            ]
        })
        pos = await md.fetch_position("BTC-PERP")
        assert pos.liquidation_price is None

    @pytest.mark.asyncio
    async def test_fetch_exchange_pnl(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={
            "assetPositions": [
                {"position": {"coin": "BTC", "unrealizedPnl": "250.75"}, "type": "oneWay"}
            ]
        })
        pnl = await md.fetch_exchange_pnl("BTC-PERP")
        assert pnl == pytest.approx(250.75)

    @pytest.mark.asyncio
    async def test_fetch_exchange_pnl_no_position(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(return_value={"assetPositions": []})
        pnl = await md.fetch_exchange_pnl("BTC-PERP")
        assert pnl == 0.0

    @pytest.mark.asyncio
    async def test_fetch_exchange_pnl_error(self, md_with_info: MarketData):
        md = md_with_info
        md._info.user_state = MagicMock(side_effect=Exception("API error"))
        pnl = await md.fetch_exchange_pnl("BTC-PERP")
        assert pnl == 0.0

    @pytest.mark.asyncio
    async def test_fetch_book_depth(self, md_with_info: MarketData):
        md = md_with_info
        md._mid_prices["BTC-PERP"] = 60000.0
        md._info.l2_snapshot = MagicMock(return_value={
            "levels": [
                # Bids
                [
                    {"px": "59998.0", "sz": "1.0", "n": 1},
                    {"px": "59990.0", "sz": "2.0", "n": 1},
                    {"px": "59900.0", "sz": "5.0", "n": 1},  # Outside 5bps
                ],
                # Asks
                [
                    {"px": "60002.0", "sz": "1.5", "n": 1},
                    {"px": "60010.0", "sz": "3.0", "n": 1},
                    {"px": "60100.0", "sz": "10.0", "n": 1},  # Outside 5bps
                ],
            ]
        })
        depth = await md.fetch_book_depth("BTC-PERP", depth_bps=5.0)
        # 5bps of 60000 = 30.0
        # Bids within 30: 59998 (diff=2), 59990 (diff=10). 59900 (diff=100) excluded.
        # Asks within 30: 60002 (diff=2), 60010 (diff=10). 60100 (diff=100) excluded.
        expected = 1.0 + 2.0 + 1.5 + 3.0
        assert depth == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_fetch_book_depth_no_mid(self, md_with_info: MarketData):
        md = md_with_info
        md._info.l2_snapshot = MagicMock(return_value={"levels": [[], []]})
        depth = await md.fetch_book_depth("BTC-PERP")
        assert depth == 0.0

    def _setup_book_snapshot(self, md: MarketData) -> None:
        """Set up a standard book for side-filtering tests."""
        md._mid_prices["BTC-PERP"] = 60000.0
        md._info.l2_snapshot = MagicMock(return_value={
            "levels": [
                [  # Bids
                    {"px": "59998.0", "sz": "1.0", "n": 1},
                    {"px": "59990.0", "sz": "2.0", "n": 1},
                ],
                [  # Asks
                    {"px": "60002.0", "sz": "1.5", "n": 1},
                    {"px": "60010.0", "sz": "3.0", "n": 1},
                ],
            ]
        })

    @pytest.mark.asyncio
    async def test_fetch_book_depth_sell_side_only(self, md_with_info: MarketData):
        """SELL → sum bid-side depth (you sell into resting bids)."""
        md = md_with_info
        self._setup_book_snapshot(md)
        depth = await md.fetch_book_depth("BTC-PERP", depth_bps=5.0, side=OrderSide.SELL)
        # Bids within 30 of mid (60000): 59998 (diff=2), 59990 (diff=10)
        assert depth == pytest.approx(1.0 + 2.0)

    @pytest.mark.asyncio
    async def test_fetch_book_depth_buy_side_only(self, md_with_info: MarketData):
        """BUY → sum ask-side depth (you buy from resting asks)."""
        md = md_with_info
        self._setup_book_snapshot(md)
        depth = await md.fetch_book_depth("BTC-PERP", depth_bps=5.0, side=OrderSide.BUY)
        # Asks within 30 of mid: 60002 (diff=2), 60010 (diff=10)
        assert depth == pytest.approx(1.5 + 3.0)

    @pytest.mark.asyncio
    async def test_fetch_book_depth_none_sums_both(self, md_with_info: MarketData):
        """side=None (default) sums both sides."""
        md = md_with_info
        self._setup_book_snapshot(md)
        depth = await md.fetch_book_depth("BTC-PERP", depth_bps=5.0, side=None)
        assert depth == pytest.approx(1.0 + 2.0 + 1.5 + 3.0)


# ===========================================================================
# 16. Initialization and buffer setup
# ===========================================================================

class TestInitialization:
    def test_buffers_created_for_each_asset(self, md2: MarketData):
        assert "BTC-PERP" in md2._return_buffers
        assert "ETH-PERP" in md2._return_buffers
        assert "BTC-PERP" in md2._minute_candles
        assert "ETH-PERP" in md2._minute_candles

    def test_initial_state(self, md: MarketData):
        assert md._ws_connected is False
        assert md._last_ws_message_ms == 0
        assert md.get_mid_price("BTC-PERP") == 0.0
        assert md.get_mark_price("BTC-PERP") == 0.0

    def test_no_wallet_address(self):
        cfg = BotConfig(testnet=True, wallet_address="")
        md = MarketData(cfg)
        assert md._config.wallet_address == ""

    def test_return_buffer_maxlen(self, md: MarketData):
        buf = md._return_buffers["BTC-PERP"]
        assert buf.maxlen == 10_000

    def test_candle_buffer_maxlen(self, md: MarketData):
        buf = md._minute_candles["BTC-PERP"]
        assert buf.maxlen == 10_080


# ===========================================================================
# 17. Config changes
# ===========================================================================

class TestConfigChanges:
    def test_wallet_address_in_config(self):
        cfg = BotConfig(wallet_address="0xabc123")
        assert cfg.wallet_address == "0xabc123"

    def test_base_url_mainnet(self):
        cfg = BotConfig(testnet=False)
        assert "hyperliquid.xyz" in cfg.base_url
        assert "testnet" not in cfg.base_url

    def test_base_url_testnet(self):
        cfg = BotConfig(testnet=True)
        assert "testnet" in cfg.base_url

    def test_wallet_address_default_empty(self):
        cfg = BotConfig()
        assert cfg.wallet_address == ""
