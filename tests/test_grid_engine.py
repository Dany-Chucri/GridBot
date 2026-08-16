"""Tests for GridEngine — pure calculation, no mocks needed."""

import pytest

from gridbot.config import AssetConfig, OperationalConfig
from gridbot.grid_engine import (
    CORE_RANGE_ATR,
    MAKER_FEE_BPS,
    SAFETY_MARGIN_BPS,
    GridEngine,
)
from gridbot.types import (
    AssetState,
    DesiredOrder,
    GridConfig,
    GridLayer,
    GridLevel,
    InventoryZone,
    OrderSide,
    PendingFlip,
    Position,
    Regime,
    TimeInForce,
    VolMetrics,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _vol(
    realized_vol: float = 0.50,
    atr: float = 500.0,
    spread_bps: float = 1.0,
    baseline_vol: float = 0.50,
) -> VolMetrics:
    """Convenience builder for VolMetrics with sensible defaults."""
    return VolMetrics(
        realized_vol=realized_vol,
        atr=atr,
        spread_bps=spread_bps,
        rolling_return_1m=0.0,
        rolling_return_5m=0.0,
        baseline_vol=baseline_vol,
    )


def _cfg(**overrides) -> AssetConfig:
    """BTC config with sensible test defaults."""
    defaults = dict(
        symbol="BTC-PERP",
        levels_per_side=5,  # smaller for test readability
        expansion_levels_per_side=3,
        max_abs_position=1.0,
        grid_step_bps_min=15.0,
        grid_step_bps_max=25.0,
    )
    defaults.update(overrides)
    return AssetConfig(**defaults)


def _op_cfg(**overrides) -> OperationalConfig:
    defaults = dict(stagger_initial_levels=5)
    defaults.update(overrides)
    return OperationalConfig(**defaults)


def _engine(cfg=None, op_cfg=None) -> GridEngine:
    return GridEngine(cfg or _cfg(), op_cfg or _op_cfg())


def _grid_config(
    symbol: str = "BTC-PERP",
    anchor: float = 50000.0,
    step_bps: float = 20.0,
    epoch: int = 1,
) -> GridConfig:
    return GridConfig(
        symbol=symbol, anchor=anchor, range_atr=CORE_RANGE_ATR,
        step_bps=step_bps, epoch=epoch,
    )


def _state(
    mid_price: float = 50000.0,
    regime: Regime = Regime.RANGE,
    position_size: float = 0.0,
    account_equity: float = 100_000.0,
    anchor: float = 50000.0,
    vol: VolMetrics | None = None,
    pending_flips: list[PendingFlip] | None = None,
    stagger_placed_count: int = 0,
) -> AssetState:
    pos = Position(symbol="BTC-PERP", size=position_size, avg_entry_price=anchor,
                   unrealized_pnl=0.0) if position_size != 0.0 else None
    return AssetState(
        symbol="BTC-PERP",
        mid_price=mid_price,
        regime=regime,
        position=pos,
        vol_metrics=vol or _vol(),
        grid_config=_grid_config(anchor=anchor),
        account_equity=account_equity,
        pending_flips=pending_flips or [],
        stagger_placed_count=stagger_placed_count,
    )


# ===========================================================================
# 7.3 — Deterministic order IDs (existing tests kept + expanded)
# ===========================================================================

class TestClientOrderId:
    """Deterministic order ID generation (section 7.3)."""

    def test_same_inputs_produce_same_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        assert id1 == id2

    def test_different_price_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50100.0, OrderSide.BUY, "abc123", 1)
        assert id1 != id2

    def test_different_config_hash_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "def456", 1)
        assert id1 != id2

    def test_different_epoch_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 2)
        assert id1 != id2

    def test_different_side_produces_different_id(self):
        id1 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        id2 = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.SELL, "abc123", 1)
        assert id1 != id2

    def test_id_is_valid_cloid_format(self):
        cid = GridEngine.make_client_order_id("BTC-PERP", 50000.0, OrderSide.BUY, "abc123", 1)
        assert cid.startswith("0x")
        assert len(cid) == 34
        int(cid[2:], 16)  # must be valid hex


class TestConfigHash:
    def test_deterministic(self):
        h1 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        h2 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        assert h1 == h2

    def test_different_anchor(self):
        h1 = GridEngine.compute_config_hash(50000.0, 2.5, 20.0)
        h2 = GridEngine.compute_config_hash(51000.0, 2.5, 20.0)
        assert h1 != h2


# ===========================================================================
# 1.1 — Grid spacing formula
# ===========================================================================

class TestGridSpacing:
    """Section 5.4 + 5.7: fee+spread+slippage-aware step."""

    def test_step_equals_atr_value_when_frictions_small(self):
        """When friction floor is below ATR step, ATR step dominates."""
        eng = _engine()
        # atr/price*10000 = 100/50000*10000 = 20 bps -> within [15, 25]
        vol = _vol(realized_vol=0.50, atr=100.0, baseline_vol=0.50, spread_bps=0.5)
        step = eng.compute_effective_step(vol, mid_price=50000.0)
        # friction = 2*0.2 + 0.5 + 1.5 + 1.5 = 3.9 bps
        # 20 > 3.9, so step should be 20
        assert step == pytest.approx(20.0)

    def test_step_widens_to_friction_floor(self):
        """When spread is huge, friction floor dominates."""
        eng = _engine()
        # atr_step = 100/50000*10000 = 20 bps
        vol = _vol(realized_vol=0.50, atr=100.0, baseline_vol=0.50, spread_bps=20.0)
        step = eng.compute_effective_step(vol, mid_price=50000.0)
        # friction = 0.4 + 20.0 + 1.5 + 1.5 = 23.4 bps
        # max(20, 23.4) = 23.4, clamped to [15, 25] = 23.4
        assert step == pytest.approx(23.4)

    def test_step_clamped_to_max(self):
        """Friction floor exceeding max is clamped."""
        eng = _engine()
        vol = _vol(realized_vol=0.50, atr=100.0, baseline_vol=0.50, spread_bps=30.0)
        step = eng.compute_effective_step(vol, mid_price=50000.0)
        # friction = 0.4 + 30 + 1.5 + 1.5 = 33.4 -> clamped to 25
        assert step == pytest.approx(25.0)

    def test_step_clamped_to_min(self):
        """Low ATR with tiny frictions still respects min."""
        eng = _engine()
        # atr/price*10000 = 50/50000*10000 = 10 bps -> clamped up to min (15)
        vol = _vol(realized_vol=0.25, atr=50.0, baseline_vol=0.50, spread_bps=0.1)
        step = eng.compute_effective_step(vol, mid_price=50000.0)
        assert step == pytest.approx(15.0)

    def test_high_atr_clamped_to_max(self):
        """Large ATR/price ratio clamped to max step."""
        eng = _engine()
        # atr/price*10000 = 500/50000*10000 = 100 bps -> clamped to 25
        vol = _vol(realized_vol=0.50, atr=500.0, baseline_vol=0.50, spread_bps=0.5)
        step = eng.compute_effective_step(vol, mid_price=50000.0)
        assert step == pytest.approx(25.0)

    def test_slippage_buffer_increases_with_elevated_vol(self):
        eng = _engine()
        normal_buf = eng._compute_grid_slippage_buffer(_vol(realized_vol=0.50, baseline_vol=0.50))
        high_buf = eng._compute_grid_slippage_buffer(_vol(realized_vol=1.00, baseline_vol=0.50))
        assert high_buf > normal_buf

    def test_slippage_buffer_at_baseline_equals_base(self):
        eng = _engine()
        buf = eng._compute_grid_slippage_buffer(_vol(realized_vol=0.50, baseline_vol=0.50))
        assert buf == pytest.approx(1.5)  # base_slippage_bps

    def test_slippage_buffer_clamped_to_floor(self):
        eng = _engine()
        buf = eng._compute_grid_slippage_buffer(_vol(realized_vol=0.10, baseline_vol=0.50))
        assert buf == pytest.approx(1.0)  # min_slippage_bps

    def test_slippage_buffer_clamped_to_cap(self):
        eng = _engine()
        buf = eng._compute_grid_slippage_buffer(_vol(realized_vol=5.0, baseline_vol=0.50))
        assert buf == pytest.approx(10.0)  # max_slippage_bps

    def test_slippage_buffer_no_baseline_returns_max(self):
        eng = _engine()
        buf = eng._compute_grid_slippage_buffer(_vol(baseline_vol=0.0))
        assert buf == pytest.approx(10.0)


# ===========================================================================
# 1.2 — Order sizing
# ===========================================================================

class TestOrderSizing:
    """Section 5.5: vol-inverse sizing."""

    def test_size_decreases_with_higher_vol(self):
        # Equity chosen so raw sizes land between min and max clamps
        # target_risk = 0.70 * 10 / 5 = 1.4
        # low: 1.4 / 0.30 = 4.67, high: 1.4 / 0.90 = 1.56
        cfg = _cfg(max_abs_position=100.0)
        eng = _engine(cfg=cfg)
        low_vol = _vol(realized_vol=0.30)
        high_vol = _vol(realized_vol=0.90)
        s_low = eng._compute_order_size(low_vol, 10.0)
        s_high = eng._compute_order_size(high_vol, 10.0)
        assert s_low > s_high

    def test_size_clamped_at_min(self):
        # Huge vol + tiny equity so the raw size falls below min
        cfg = _cfg(max_abs_position=10000.0)
        eng = _engine(cfg=cfg)
        vol = _vol(realized_vol=100.0)
        size = eng._compute_order_size(vol, 0.01)  # tiny equity -> 0.70*0.01/5/100 = 0.000014
        assert size == pytest.approx(0.001)  # BTC min

    def test_size_clamped_at_max(self):
        eng = _engine()
        # Tiny vol -> huge order -> clamped to max_abs/levels
        vol = _vol(realized_vol=0.01)
        size = eng._compute_order_size(vol, 100_000.0)
        assert size == pytest.approx(1.0 / 5)  # max_abs_position / levels_per_side

    def test_near_zero_vol_no_infinity(self):
        eng = _engine()
        vol = _vol(realized_vol=0.0)
        size = eng._compute_order_size(vol, 100_000.0)
        assert size > 0
        assert size < float("inf")

    def test_zero_equity_returns_min(self):
        eng = _engine()
        size = eng._compute_order_size(_vol(), 0.0)
        assert size == pytest.approx(0.001)

    def test_eth_min_size(self):
        cfg = _cfg(symbol="ETH-PERP", max_abs_position=100.0)
        eng = _engine(cfg=cfg)
        vol = _vol(realized_vol=100.0)
        size = eng._compute_order_size(vol, 1.0)  # tiny equity to hit min
        assert size == pytest.approx(0.01)

    def test_expansion_uses_expansion_allocation(self):
        """Expansion sizing uses expansion_allocation, not capital_allocation."""
        cfg = _cfg(max_abs_position=1000.0)
        eng = _engine(cfg=cfg)
        vol = _vol(realized_vol=0.50)
        # Use small equity so sizes don't hit max clamp
        core_size = eng._compute_order_size(vol, 10.0)
        exp_size = eng._compute_expansion_order_size(vol, 10.0)
        # core: 0.70 * 10 / 5 / 0.50 = 2.8
        # expansion: 0.30 * 10 / 3 / 0.50 = 2.0
        assert core_size == pytest.approx(2.8)
        assert exp_size == pytest.approx(2.0)


# ===========================================================================
# 1.3 — Inventory classification and skewing
# ===========================================================================

class TestInventoryClassification:
    """Section 6.2: zone classification."""

    def test_normal_zone(self):
        eng = _engine()
        assert eng.classify_inventory_zone(0.0) == InventoryZone.NORMAL
        assert eng.classify_inventory_zone(0.3) == InventoryZone.NORMAL
        assert eng.classify_inventory_zone(-0.3) == InventoryZone.NORMAL

    def test_soft_cap_zone(self):
        # soft_cap = 0.5 * 1.0 = 0.5
        eng = _engine()
        assert eng.classify_inventory_zone(0.5) == InventoryZone.SOFT_CAP
        assert eng.classify_inventory_zone(0.7) == InventoryZone.SOFT_CAP
        assert eng.classify_inventory_zone(-0.5) == InventoryZone.SOFT_CAP

    def test_hard_cap_zone(self):
        eng = _engine()
        assert eng.classify_inventory_zone(1.0) == InventoryZone.HARD_CAP
        assert eng.classify_inventory_zone(1.5) == InventoryZone.HARD_CAP
        assert eng.classify_inventory_zone(-1.0) == InventoryZone.HARD_CAP

    def test_boundary_soft_cap(self):
        """Exactly at soft_cap boundary -> SOFT_CAP."""
        eng = _engine()
        assert eng.classify_inventory_zone(0.5) == InventoryZone.SOFT_CAP

    def test_boundary_hard_cap(self):
        """Exactly at hard_cap boundary -> HARD_CAP."""
        eng = _engine()
        assert eng.classify_inventory_zone(1.0) == InventoryZone.HARD_CAP


class TestInventorySkew:
    """Section 6.2: skew behavior."""

    def _make_levels(self) -> list[GridLevel]:
        return [
            GridLevel(price=49900, side=OrderSide.BUY, size=0.1, layer=GridLayer.CORE),
            GridLevel(price=49800, side=OrderSide.BUY, size=0.1, layer=GridLayer.CORE),
            GridLevel(price=50100, side=OrderSide.SELL, size=0.1, layer=GridLayer.CORE),
            GridLevel(price=50200, side=OrderSide.SELL, size=0.1, layer=GridLayer.CORE),
        ]

    def test_normal_no_change(self):
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, 0.0, InventoryZone.NORMAL)
        assert len(result) == 4
        for orig, res in zip(levels, result):
            assert res.size == orig.size

    def test_soft_cap_long_reduces_buys(self):
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, 0.75, InventoryZone.SOFT_CAP)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        for b in buys:
            assert b.size < 0.1
        for s in sells:
            assert s.size > 0.1

    def test_soft_cap_short_reduces_sells(self):
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, -0.75, InventoryZone.SOFT_CAP)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        for b in buys:
            assert b.size > 0.1
        for s in sells:
            assert s.size < 0.1

    def test_hard_cap_long_removes_buys(self):
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, 1.0, InventoryZone.HARD_CAP)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        assert len(buys) == 0
        assert len(sells) == 2
        for s in sells:
            assert s.reduce_only is True

    def test_hard_cap_short_removes_sells(self):
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, -1.0, InventoryZone.HARD_CAP)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        assert len(sells) == 0
        assert len(buys) == 2
        for b in buys:
            assert b.reduce_only is True

    def test_zero_position_hard_cap_no_removal(self):
        """Zero position at hard cap (edge case) — no side to increase."""
        eng = _engine()
        levels = self._make_levels()
        result = eng._apply_inventory_skew(levels, 0.0, InventoryZone.HARD_CAP)
        assert len(result) == 4

    def test_soft_cap_unwind_boost_clamped_to_max(self):
        """Unwind side size after skew must not exceed max_size."""
        eng = _engine()
        # At skew_factor ~1.0 (near hard cap), unwind gets size * 2.0
        # max_size = max_abs_position / levels_per_side = 1.0 / 5 = 0.2
        levels = [
            GridLevel(price=49900, side=OrderSide.BUY, size=0.15, layer=GridLayer.CORE),
            GridLevel(price=50100, side=OrderSide.SELL, size=0.15, layer=GridLayer.CORE),
        ]
        # position=0.99 -> just under hard cap (1.0), skew_factor ~ 0.98
        result = eng._apply_inventory_skew(levels, 0.99, InventoryZone.SOFT_CAP)
        sells = [l for l in result if l.side == OrderSide.SELL]
        for s in sells:
            assert s.size <= 0.2  # max_abs_position / levels_per_side


# ===========================================================================
# 1.4 — Core grid level computation
# ===========================================================================

class TestCoreLevels:
    """Section 5.2, Layer 1."""

    def test_correct_number_of_levels(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        buys = [l for l in levels if l.side == OrderSide.BUY]
        sells = [l for l in levels if l.side == OrderSide.SELL]
        assert len(buys) == 5  # levels_per_side=5
        assert len(sells) == 5

    def test_levels_symmetric_around_anchor(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        buys = sorted([l for l in levels if l.side == OrderSide.BUY], key=lambda l: -l.price)
        sells = sorted([l for l in levels if l.side == OrderSide.SELL], key=lambda l: l.price)
        for b, s in zip(buys, sells):
            assert abs((50000 - b.price) - (s.price - 50000)) < 0.01

    def test_buy_levels_below_anchor(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        for l in levels:
            if l.side == OrderSide.BUY:
                assert l.price < 50000.0

    def test_sell_levels_above_anchor(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        for l in levels:
            if l.side == OrderSide.SELL:
                assert l.price > 50000.0

    def test_levels_spaced_by_step(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        step_bps = 20.0
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=step_bps, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        buys = sorted([l for l in levels if l.side == OrderSide.BUY], key=lambda l: -l.price)
        expected_step = 50000.0 * step_bps / 10_000
        for i in range(len(buys)):
            expected_price = 50000.0 * (1 - (i + 1) * step_bps / 10_000)
            assert buys[i].price == pytest.approx(expected_price, rel=1e-9)

    def test_no_levels_outside_core_range(self):
        """Tiny ATR causes most levels to fall outside range."""
        eng = _engine()
        vol = _vol(atr=10.0)  # very small ATR -> core range = 25
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        core_range = CORE_RANGE_ATR * 10.0  # 25
        for l in levels:
            assert abs(l.price - 50000.0) <= core_range + 0.01

    def test_all_levels_are_core_layer(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_core_levels(
            anchor=50000.0, step_bps=20.0, vol_metrics=vol,
            inventory_zone=InventoryZone.NORMAL, position_size=0.0,
            order_size=0.1,
        )
        for l in levels:
            assert l.layer == GridLayer.CORE


# ===========================================================================
# 1.5 — Expansion grid level computation
# ===========================================================================

class TestExpansionLevels:
    """Section 5.2, Layer 2."""

    def test_empty_when_mid_within_core_range(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=50000.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        assert levels == []

    def test_empty_when_mid_beyond_breakout(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        # breakout_atr_distance=4.5, so breakout = 2250. Mid at anchor+2300 -> beyond
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=52300.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        assert levels == []

    def test_active_when_mid_between_core_and_breakout(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        # core_range = 1250, breakout = 2250
        # mid at anchor + 1500 -> between core and breakout
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=51500.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        assert len(levels) > 0

    def test_expansion_step_is_wider(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=51500.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        # All levels should be expansion layer
        for l in levels:
            assert l.layer == GridLayer.EXPANSION

    def test_levels_start_at_core_boundary(self):
        eng = _engine()
        vol = _vol(atr=500.0)
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=51500.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        core_range = CORE_RANGE_ATR * 500.0  # 1250
        # The closest levels to anchor should be near core_range boundary
        buys = [l for l in levels if l.side == OrderSide.BUY]
        sells = [l for l in levels if l.side == OrderSide.SELL]
        if buys:
            closest_buy = max(buys, key=lambda l: l.price)
            assert abs(closest_buy.price - (50000 - core_range)) < 50000 * 30 / 10_000 + 1
        if sells:
            closest_sell = min(sells, key=lambda l: l.price)
            assert abs(closest_sell.price - (50000 + core_range)) < 50000 * 30 / 10_000 + 1

    def test_empty_with_zero_atr(self):
        eng = _engine()
        vol = _vol(atr=0.0)
        levels = eng._compute_expansion_levels(
            anchor=50000.0, mid_price=51500.0, step_bps=20.0,
            vol_metrics=vol, inventory_zone=InventoryZone.NORMAL,
            position_size=0.0, account_equity=100_000.0,
        )
        assert levels == []


# ===========================================================================
# 1.6 — Staggered placement
# ===========================================================================

class TestStaggeredPlacement:
    """Section 5.3."""

    def _make_many_levels(self, anchor: float = 50000.0) -> list[GridLevel]:
        levels = []
        for i in range(1, 11):
            levels.append(GridLevel(
                price=anchor - i * 10, side=OrderSide.BUY, size=0.1,
                layer=GridLayer.CORE,
            ))
            levels.append(GridLevel(
                price=anchor + i * 10, side=OrderSide.SELL, size=0.1,
                layer=GridLayer.CORE,
            ))
        return levels

    def test_only_nearest_n_per_side(self):
        eng = _engine(op_cfg=_op_cfg(stagger_initial_levels=3))
        levels = self._make_many_levels()
        result = eng._apply_stagger(levels, 50000.0, placed_count=0)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        assert len(buys) == 3
        assert len(sells) == 3

    def test_farther_levels_excluded(self):
        eng = _engine(op_cfg=_op_cfg(stagger_initial_levels=3))
        levels = self._make_many_levels()
        result = eng._apply_stagger(levels, 50000.0, placed_count=0)
        for l in result:
            assert abs(l.price - 50000) <= 30 + 0.01

    def test_fill_promotion_includes_more(self):
        eng = _engine(op_cfg=_op_cfg(stagger_initial_levels=3))
        levels = self._make_many_levels()
        result = eng._apply_stagger(levels, 50000.0, placed_count=2)
        buys = [l for l in result if l.side == OrderSide.BUY]
        sells = [l for l in result if l.side == OrderSide.SELL]
        assert len(buys) == 5  # 3 + 2
        assert len(sells) == 5

    def test_stagger_with_all_levels_allowed(self):
        """When stagger allows all levels, nothing is dropped."""
        eng = _engine(op_cfg=_op_cfg(stagger_initial_levels=10))
        levels = self._make_many_levels()
        result = eng._apply_stagger(levels, 50000.0, placed_count=0)
        assert len(result) == 20  # all 10 per side


# ===========================================================================
# 1.7 — Anchor management
# ===========================================================================

class TestAnchorManagement:
    """Section 5.1: four-condition re-anchor gate."""

    def _defaults(self):
        return dict(
            mid_price=51000.0,
            current_anchor=50000.0,
            atr=500.0,
            regime=Regime.RANGE,
            vol_stable=True,
            drift_start_ms=0,
            now_ms=60 * 60 * 1000,  # 60 min elapsed
        )

    def test_all_conditions_true(self):
        eng = _engine()
        assert eng.should_reanchor(**self._defaults()) is True

    def test_fails_wrong_regime(self):
        eng = _engine()
        args = self._defaults()
        args["regime"] = Regime.TREND
        assert eng.should_reanchor(**args) is False

    def test_fails_vol_unstable(self):
        eng = _engine()
        args = self._defaults()
        args["vol_stable"] = False
        assert eng.should_reanchor(**args) is False

    def test_fails_drift_too_small(self):
        eng = _engine()
        args = self._defaults()
        args["mid_price"] = 50100.0  # drift=100, threshold=750
        assert eng.should_reanchor(**args) is False

    def test_fails_delay_not_elapsed(self):
        eng = _engine()
        args = self._defaults()
        args["now_ms"] = 10 * 60 * 1000  # 10 min < 30 min delay
        assert eng.should_reanchor(**args) is False

    def test_fails_no_drift_start(self):
        eng = _engine()
        args = self._defaults()
        args["drift_start_ms"] = None
        assert eng.should_reanchor(**args) is False

    def test_compute_new_anchor_returns_mid(self):
        eng = _engine()
        assert eng.compute_new_anchor(51000.0, 50000.0) == 51000.0


# ===========================================================================
# 1.8 — Pending flips
# ===========================================================================

class TestPendingFlips:
    """Section 7.6."""

    def test_flips_appear_in_output(self):
        eng = _engine()
        gc = _grid_config()
        flips = [PendingFlip(price=50100, side=OrderSide.SELL, size=0.1, originating_fill_id="f1")]
        result = eng._include_pending_flips([], flips, gc, InventoryZone.NORMAL)
        assert len(result) == 1
        assert result[0].price == 50100
        assert result[0].side == OrderSide.SELL

    def test_dedup_when_same_price_side(self):
        eng = _engine()
        gc = _grid_config()
        existing = [DesiredOrder(
            client_order_id="abc", symbol="BTC-PERP", price=50100,
            size=0.1, side=OrderSide.SELL,
        )]
        flips = [PendingFlip(price=50100, side=OrderSide.SELL, size=0.1, originating_fill_id="f1")]
        result = eng._include_pending_flips(existing, flips, gc, InventoryZone.NORMAL)
        assert len(result) == 1  # no duplicate

    def test_hard_cap_converts_increasing_to_reduce_only(self):
        eng = _engine()
        gc = _grid_config()
        # Long position -> buy is exposure-increasing
        flips = [PendingFlip(price=49900, side=OrderSide.BUY, size=0.1, originating_fill_id="f1")]
        result = eng._include_pending_flips(
            [], flips, gc, InventoryZone.HARD_CAP, position_size=1.0,
        )
        assert len(result) == 1
        assert result[0].reduce_only is True

    def test_hard_cap_unwind_side_not_reduce_only(self):
        eng = _engine()
        gc = _grid_config()
        # Long position -> sell is unwind side
        flips = [PendingFlip(price=50100, side=OrderSide.SELL, size=0.1, originating_fill_id="f1")]
        result = eng._include_pending_flips(
            [], flips, gc, InventoryZone.HARD_CAP, position_size=1.0,
        )
        assert len(result) == 1
        assert result[0].reduce_only is False

    def test_empty_flips_no_change(self):
        eng = _engine()
        gc = _grid_config()
        existing = [DesiredOrder(
            client_order_id="abc", symbol="BTC-PERP", price=50100,
            size=0.1, side=OrderSide.SELL,
        )]
        result = eng._include_pending_flips(existing, [], gc, InventoryZone.NORMAL)
        assert result == existing

    def test_flip_order_has_alo_tif(self):
        eng = _engine()
        gc = _grid_config()
        flips = [PendingFlip(price=50100, side=OrderSide.SELL, size=0.1, originating_fill_id="f1")]
        result = eng._include_pending_flips([], flips, gc, InventoryZone.NORMAL)
        assert result[0].time_in_force == TimeInForce.ALO


# ===========================================================================
# 1.9 — Main entry point: compute_desired_orders
# ===========================================================================

class TestComputeDesiredOrders:
    """Full orchestration tests."""

    def test_empty_for_trend_regime(self):
        eng = _engine()
        state = _state(regime=Regime.TREND)
        assert eng.compute_desired_orders(state) == []

    def test_empty_for_high_vol_regime(self):
        eng = _engine()
        state = _state(regime=Regime.HIGH_VOL)
        assert eng.compute_desired_orders(state) == []

    def test_empty_for_unknown_regime(self):
        eng = _engine()
        state = _state(regime=Regime.UNKNOWN)
        assert eng.compute_desired_orders(state) == []

    def test_returns_orders_for_range(self):
        eng = _engine()
        state = _state(regime=Regime.RANGE)
        orders = eng.compute_desired_orders(state)
        assert len(orders) > 0

    def test_force_reduce_only_treats_zone_as_hard_cap(self):
        """Section 9.2: a portfolio delta cap breach must force hard-cap
        behavior (no exposure-increasing orders) even when this asset's own
        position is well within its individual soft/hard cap."""
        eng = _engine()
        state = _state(regime=Regime.RANGE, position_size=0.1)  # well under caps
        state.force_reduce_only = True
        orders = eng.compute_desired_orders(state)
        # Long position -> BUY is exposure-increasing; none should remain
        assert all(o.side != OrderSide.BUY for o in orders)

    def test_force_reduce_only_false_allows_full_grid(self):
        eng = _engine()
        state = _state(regime=Regime.RANGE, position_size=0.1)
        state.force_reduce_only = False
        orders = eng.compute_desired_orders(state)
        assert any(o.side == OrderSide.BUY for o in orders)

    def test_all_orders_have_alo(self):
        eng = _engine()
        state = _state()
        orders = eng.compute_desired_orders(state)
        for o in orders:
            assert o.time_in_force == TimeInForce.ALO

    def test_all_order_ids_deterministic(self):
        eng = _engine()
        state = _state()
        orders1 = eng.compute_desired_orders(state)
        orders2 = eng.compute_desired_orders(state)
        ids1 = [o.client_order_id for o in orders1]
        ids2 = [o.client_order_id for o in orders2]
        assert ids1 == ids2

    def test_no_duplicate_order_ids(self):
        eng = _engine()
        state = _state()
        orders = eng.compute_desired_orders(state)
        ids = [o.client_order_id for o in orders]
        assert len(ids) == len(set(ids))

    def test_includes_expansion_when_mid_in_zone(self):
        # Use high stagger limit so expansion levels aren't cut
        eng = _engine(op_cfg=_op_cfg(stagger_initial_levels=25))
        # Mid at anchor + 1500, core_range=1250, breakout=2250
        state = _state(mid_price=51500.0, anchor=50000.0, vol=_vol(atr=500.0))
        orders = eng.compute_desired_orders(state)
        # Should have both core and expansion orders (more than just 10 core)
        assert len(orders) > 10

    def test_inventory_skew_applied(self):
        eng = _engine()
        state = _state(position_size=1.0)  # at hard cap
        orders = eng.compute_desired_orders(state)
        buys = [o for o in orders if o.side == OrderSide.BUY]
        assert len(buys) == 0  # all buys removed at hard cap

    def test_pending_flips_included(self):
        eng = _engine()
        flips = [PendingFlip(price=50200, side=OrderSide.SELL, size=0.05, originating_fill_id="f1")]
        state = _state(pending_flips=flips)
        orders = eng.compute_desired_orders(state)
        flip_at_price = [o for o in orders if o.price == 50200 and o.side == OrderSide.SELL]
        assert len(flip_at_price) >= 1

    def test_empty_when_no_vol_metrics(self):
        eng = _engine()
        state = _state()
        state.vol_metrics = None
        assert eng.compute_desired_orders(state) == []

    def test_empty_when_no_grid_config(self):
        eng = _engine()
        state = _state()
        state.grid_config = None
        assert eng.compute_desired_orders(state) == []

    def test_symbol_on_all_orders(self):
        eng = _engine()
        state = _state()
        orders = eng.compute_desired_orders(state)
        for o in orders:
            assert o.symbol == "BTC-PERP"
