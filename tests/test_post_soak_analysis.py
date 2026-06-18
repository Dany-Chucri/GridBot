"""Smoke test for scripts/post_soak_analysis.py (Phase 8.4)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from gridbot.state_store import StateStore
from gridbot.types import (
    AssetState,
    BotState,
    Fill,
    GridConfig,
    OrderSide,
    Position,
    Regime,
)

# Make scripts/ importable as a module path
_SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import post_soak_analysis  # noqa: E402


@pytest.mark.asyncio
async def test_report_builds_from_real_state_store(tmp_path: Path):
    """Populate a temp DB with representative data and verify the report
    contains all expected sections and numeric totals."""
    db_path = tmp_path / "gridbot.db"
    store = StateStore(db_path)
    await store.initialize()

    # Populate: fills (one buy open, one sell close for a +$10 profit)
    await store.record_fill(
        Fill(
            fill_id="f1", order_id=1, client_order_id="c1",
            symbol="BTC-PERP", price=50_000.0, size=0.1,
            side=OrderSide.BUY, fee=0.25, timestamp_ms=1_000,
            is_maker=True, is_partial=False,
        )
    )
    await store.record_fill(
        Fill(
            fill_id="f2", order_id=2, client_order_id="c2",
            symbol="BTC-PERP", price=50_100.0, size=0.1,
            side=OrderSide.SELL, fee=0.25, timestamp_ms=2_000,
            is_maker=True, is_partial=False,
        )
    )
    # A taker fill — should flag as non-maker
    await store.record_fill(
        Fill(
            fill_id="f3", order_id=3, client_order_id="c3",
            symbol="BTC-PERP", price=50_200.0, size=0.05,
            side=OrderSide.BUY, fee=0.10, timestamp_ms=3_000,
            is_maker=False, is_partial=False,
        )
    )

    await store.save_grid_config(
        GridConfig(symbol="BTC-PERP", anchor=50_000.0, range_atr=2.5,
                   step_bps=8.0, epoch=1)
    )

    state = AssetState(symbol="BTC-PERP")
    state.bot_state = BotState.RUNNING
    state.regime = Regime.RANGE
    state.position = Position("BTC-PERP", 0.05, 50_200.0, 0.0)
    state.account_equity = 100_000.0
    await store.save_bot_state("BTC-PERP", state)
    await store.update_heartbeat("BTC-PERP", int(time.time() * 1000))

    await store.close()

    # Build the report
    now_ms = int(time.time() * 1000)
    report = post_soak_analysis.build_report(db_path, since_ms=None, now_ms=now_ms)

    # Sanity check: expected sections
    assert "# GridBot Post-Soak Report" in report
    assert "## Fills" in report
    assert "## Grid Config" in report
    assert "## Bot State" in report
    assert "## Heartbeat" in report
    assert "## Pending Flips" in report
    assert "## Notes on log-based metrics" in report

    # Expected rows
    assert "BTC-PERP" in report
    # Realized PnL: buy 0.1 @ 50000, sell 0.1 @ 50100 -> +$10 realized
    assert "10.0000" in report or "10.000" in report
    # Taker note should fire because f3 is non-maker
    assert "taker fills observed" in report
    # Grid config row
    assert "8.00" in report


def test_apply_fill_avg_cost_increase_then_close():
    """Average-cost arithmetic should yield exactly the same realized PnL
    as PnLMonitor when the sequence closes fully."""
    s = post_soak_analysis._PnLState()
    post_soak_analysis._apply_fill(s, price=100.0, size=1.0, side="buy",
                                   fee=0.1, is_maker=1)
    post_soak_analysis._apply_fill(s, price=100.0, size=1.0, side="buy",
                                   fee=0.1, is_maker=1)
    # Avg entry should be 100.0; position 2.0
    assert s.position == pytest.approx(2.0)
    assert s.avg_entry == pytest.approx(100.0)

    post_soak_analysis._apply_fill(s, price=110.0, size=2.0, side="sell",
                                   fee=0.2, is_maker=1)
    # Realized: (110 - 100) * 2 = 20
    assert s.realized == pytest.approx(20.0)
    assert s.position == pytest.approx(0.0)


def test_apply_fill_flip_through_zero():
    """A fill that reverses the sign of position realizes the closing
    portion and re-opens at the fill price."""
    s = post_soak_analysis._PnLState()
    post_soak_analysis._apply_fill(s, price=100.0, size=1.0, side="buy",
                                   fee=0.0, is_maker=1)
    # Sell 3 @ 110: closes 1 long (pnl +10), opens 2 short at 110
    post_soak_analysis._apply_fill(s, price=110.0, size=3.0, side="sell",
                                   fee=0.0, is_maker=1)
    assert s.realized == pytest.approx(10.0)
    assert s.position == pytest.approx(-2.0)
    assert s.avg_entry == pytest.approx(110.0)


def test_report_with_empty_db(tmp_path: Path):
    """An empty DB produces a report without crashing."""
    import asyncio

    async def _init():
        store = StateStore(tmp_path / "empty.db")
        await store.initialize()
        await store.close()

    asyncio.run(_init())

    report = post_soak_analysis.build_report(
        tmp_path / "empty.db", since_ms=None, now_ms=int(time.time() * 1000)
    )
    assert "No fills in window" in report
    assert "No grid config persisted" in report
