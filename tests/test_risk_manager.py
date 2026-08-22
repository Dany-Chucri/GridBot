"""Tests for RiskManager, safety constraint enforcement."""

import pytest

from gridbot.config import AssetConfig, BotConfig, OperationalConfig, PortfolioConfig
from gridbot.risk_manager import RiskAction, RiskDecision, RiskManager
from gridbot.types import (
    AssetState,
    GridConfig,
    Position,
    Regime,
    VolMetrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vol(
    realized_vol: float = 0.50,
    atr: float = 500.0,
    spread_bps: float = 1.0,
    rolling_return_1m: float = 0.0,
    rolling_return_5m: float = 0.0,
    baseline_vol: float = 0.50,
) -> VolMetrics:
    return VolMetrics(
        realized_vol=realized_vol,
        atr=atr,
        spread_bps=spread_bps,
        rolling_return_1m=rolling_return_1m,
        rolling_return_5m=rolling_return_5m,
        baseline_vol=baseline_vol,
    )


def _cfg(**overrides) -> AssetConfig:
    defaults = dict(
        symbol="BTC-PERP",
        max_abs_position=1.0,
        soft_cap_pct=0.50,
        breakout_atr_distance=4.5,
        cooldown_minutes=30.0,
        vol_pause_percentile=0.80,
        vol_kill_percentile=0.95,
        vol_recovery_minutes=10.0,
        funding_moderate_threshold=0.30,
        funding_extreme_threshold=1.00,
        max_daily_drawdown_pct=0.03,
        max_weekly_drawdown_pct=0.07,
    )
    defaults.update(overrides)
    return AssetConfig(**defaults)


def _bot_config(asset_cfg=None, **op_overrides) -> BotConfig:
    ac = asset_cfg or _cfg()
    op_defaults = dict(max_consecutive_errors=10, max_time_desynced_seconds=30.0)
    op_defaults.update(op_overrides)
    return BotConfig(
        assets=[ac],
        portfolio=PortfolioConfig(),
        operational=OperationalConfig(**op_defaults),
    )


def _rm(bot_cfg=None) -> RiskManager:
    return RiskManager(bot_cfg or _bot_config())


def _pos(size: float = 0.0, avg_entry: float = 50000.0, upnl: float = 0.0) -> Position:
    return Position(
        symbol="BTC-PERP",
        size=size,
        avg_entry_price=avg_entry,
        unrealized_pnl=upnl,
    )


def _grid_config(anchor: float = 50000.0) -> GridConfig:
    return GridConfig(
        symbol="BTC-PERP", anchor=anchor, range_atr=2.5, step_bps=20.0, epoch=1,
    )


def _state(**overrides) -> AssetState:
    defaults = dict(
        symbol="BTC-PERP",
        mid_price=50000.0,
        account_equity=100000.0,
        vol_metrics=_vol(),
        grid_config=_grid_config(),
    )
    defaults.update(overrides)
    return AssetState(**defaults)


# Max gap RiskManager treats as "continuous" (mirrors
# RiskManager._MAX_CONTINUOUS_GAP_MS). Seeded histories must stay under
# this between consecutive samples, or _continuous_run only sees the
# trailing segment, same as it would for a real gap/outage.
_SAFE_STEP_MS = 4 * 60 * 1000


def _dense_span(span_ms: int, vol_low: float, vol_high: float | None = None,
                 start_ms: int = 0) -> list[tuple[int, float]]:
    """Build a vol-history list spanning span_ms with no internal gap
    approaching the continuity threshold, so RiskManager._continuous_run
    treats it as one run, matches real record_vol() cadence (~1/s)."""
    n = max(2, span_ms // _SAFE_STEP_MS + 1)
    vh = vol_high if vol_high is not None else vol_low
    return [
        (
            start_ms + span_ms * i // (n - 1),
            vol_low + (vh - vol_low) * i / (n - 1),
        )
        for i in range(n)
    ]


# Populate vol history so percentile calculations work.
# Creates a 7-day uniform history from vol_low to vol_high.
def _seed_vol_history(rm: RiskManager, symbol: str = "BTC-PERP",
                       n: int = 100, vol_low: float = 0.30, vol_high: float = 0.70,
                       start_ms: int = 0) -> int:
    """Seed vol history and return the timestamp of the last entry.

    `n` is accepted for call-site compatibility but ignored, density is
    always set by _SAFE_STEP_MS so the run stays continuous.
    """
    del n
    span_ms = 7 * 24 * 60 * 60 * 1000
    history = _dense_span(span_ms, vol_low, vol_high, start_ms)
    rm._vol_history[symbol] = history
    return history[-1][0]


# ---------------------------------------------------------------------------
# TestRegimeDetection
# ---------------------------------------------------------------------------

class TestRegimeDetection:
    """Regime classification (section 8)."""

    def test_range_when_all_signals_agree(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm)
        cfg = _cfg()
        regime = rm.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=0.40, atr=500.0),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=last_ts, config=cfg,
        )
        assert regime == Regime.RANGE

    def test_trend_when_price_far_from_ma(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm)
        cfg = _cfg()
        # Price 3 * ATR away from MA (threshold is 2.0 * ATR)
        regime = rm.detect_regime(
            "BTC-PERP", 51500.0, _vol(realized_vol=0.40, atr=500.0),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=last_ts, config=cfg,
        )
        assert regime == Regime.TREND

    def test_high_vol_when_vol_exceeds_pause(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm)
        cfg = _cfg()
        # Vol at 0.69, 98th percentile of [0.30..0.70] uniform distribution
        regime = rm.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=0.69, atr=500.0),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=last_ts, config=cfg,
        )
        assert regime == Regime.HIGH_VOL

    def test_trend_during_cooldown(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm)
        cfg = _cfg(cooldown_minutes=30.0)
        cooldown_ms = 30 * 60 * 1000
        # Breakout happened 10 minutes ago
        regime = rm.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=0.40, atr=500.0),
            moving_avg=50000.0,
            last_breakout_ms=last_ts - 10 * 60 * 1000,
            now_ms=last_ts, config=cfg,
        )
        assert regime == Regime.TREND

    def test_range_after_cooldown_expires(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm)
        cfg = _cfg(cooldown_minutes=30.0)
        # Breakout happened 31 minutes ago
        regime = rm.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=0.40, atr=500.0),
            moving_avg=50000.0,
            last_breakout_ms=last_ts - 31 * 60 * 1000,
            now_ms=last_ts, config=cfg,
        )
        assert regime == Regime.RANGE

    def test_unknown_with_insufficient_history(self):
        rm = _rm()
        # Only 24h of history, not 48h minimum
        n = 50
        step_ms = 24 * 60 * 60 * 1000 // n
        ts = 0
        for i in range(n):
            rm.record_vol("BTC-PERP", ts, 0.50)
            ts += step_ms

        regime = rm.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=0.40),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=ts, config=_cfg(),
        )
        assert regime == Regime.UNKNOWN

    def test_bootstrap_bias_tightens_threshold(self):
        """Same vol reading returns RANGE with 7d history but HIGH_VOL with 48h history."""
        cfg = _cfg(vol_pause_percentile=0.80)

        # 7-day history: vol at 75th percentile should be RANGE (below 80th)
        rm_7d = _rm()
        _seed_vol_history(rm_7d, n=100, vol_low=0.30, vol_high=0.70)

        # 48h history: ensure span is exactly 48h (start at 0, end at 48h_ms)
        rm_48h = _rm()
        span_48h = 48 * 60 * 60 * 1000
        rm_48h._vol_history["BTC-PERP"] = _dense_span(span_48h, 0.30, 0.70)

        test_vol = 0.55  # ~62nd percentile of [0.30, 0.70]

        # With 7d: percentile ~62% < 80% pause → RANGE
        regime_7d = rm_7d.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=test_vol, atr=500.0),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=7 * 24 * 60 * 60 * 1000, config=cfg,
        )
        assert regime_7d == Regime.RANGE

        # With 48h: same percentile but bootstrap threshold is ~70%
        # A vol at ~72nd percentile should trigger HIGH_VOL under bootstrap bias
        high_test_vol = 0.59  # ~72nd percentile
        regime_48h = rm_48h.detect_regime(
            "BTC-PERP", 50000.0, _vol(realized_vol=high_test_vol, atr=500.0),
            moving_avg=50000.0, last_breakout_ms=None,
            now_ms=span_48h, config=cfg,
        )
        assert regime_48h == Regime.HIGH_VOL

    def test_bootstrap_threshold_scales_linearly(self):
        """At 48h the bias is maximal, at 7d it's zero."""
        rm = _rm()
        cfg = _cfg(vol_pause_percentile=0.80)

        # At exactly 48h
        span_48h = 48 * 60 * 60 * 1000
        rm._vol_history["BTC-PERP"] = _dense_span(span_48h, 0.50)
        threshold_48h = rm._bootstrap_adjusted_threshold("BTC-PERP", 0.80)
        assert abs(threshold_48h - 0.70) < 0.01  # maximal bias

        # At exactly 7d
        span_7d = 7 * 24 * 60 * 60 * 1000
        rm._vol_history["BTC-PERP"] = _dense_span(span_7d, 0.50)
        threshold_7d = rm._bootstrap_adjusted_threshold("BTC-PERP", 0.80)
        assert abs(threshold_7d - 0.80) < 0.01  # no bias

        # At midpoint (~3.5d from 48h mark ≈ halfway to 7d)
        span_mid = (span_48h + span_7d) // 2
        rm._vol_history["BTC-PERP"] = _dense_span(span_mid, 0.50)
        threshold_mid = rm._bootstrap_adjusted_threshold("BTC-PERP", 0.80)
        assert 0.74 < threshold_mid < 0.76  # ~halfway between 0.70 and 0.80


# ---------------------------------------------------------------------------
# TestVolHistoryContinuity, gap-aware sufficiency + persistence reload
# (project decision: vol history persistence, 5-minute continuity gap)
# ---------------------------------------------------------------------------

class TestVolHistoryContinuity:
    def test_small_gap_stays_continuous(self):
        """A gap under the 5-minute threshold (e.g. an ordinary restart)
        doesn't reset sufficiency, old and new samples count as one run."""
        rm = _rm()
        span_48h = 48 * 60 * 60 * 1000
        history = _dense_span(span_48h, 0.50)
        last_ts = history[-1][0]
        rm._vol_history["BTC-PERP"] = history

        # Simulate a restart: one more sample 4 minutes after the last one.
        rm.record_vol("BTC-PERP", last_ts + 4 * 60 * 1000, 0.50)

        assert rm._vol_history_sufficient("BTC-PERP") is True

    def test_large_gap_breaks_continuity(self):
        """A gap over the 5-minute threshold (a real outage) means only the
        trailing run counts, pre-gap history no longer satisfies the 48h
        minimum even though it's technically still in `_vol_history`."""
        rm = _rm()
        span_48h = 48 * 60 * 60 * 1000
        history = _dense_span(span_48h, 0.50)
        last_ts = history[-1][0]
        rm._vol_history["BTC-PERP"] = history

        # Simulate a 3-day outage: next sample arrives 3 days later.
        rm.record_vol("BTC-PERP", last_ts + 3 * 24 * 60 * 60 * 1000, 0.50)

        assert rm._vol_history_sufficient("BTC-PERP") is False

    def test_load_vol_history_seeds_continuous_run(self):
        """Persisted samples reloaded on restart recovery satisfy
        sufficiency immediately, without waiting through bootstrap again."""
        rm = _rm()
        span_48h = 48 * 60 * 60 * 1000
        samples = _dense_span(span_48h, 0.50, start_ms=0)
        now_ms = samples[-1][0] + 1_000  # restart shortly after the last sample

        rm.load_vol_history("BTC-PERP", samples, now_ms)

        assert rm._vol_history_sufficient("BTC-PERP") is True

    def test_load_vol_history_trims_to_7d_relative_to_now(self):
        """A long-dormant DB may hold samples individually close together
        but collectively stale relative to actual now, trim relative to
        now_ms, not the last-persisted timestamp."""
        rm = _rm()
        span_48h = 48 * 60 * 60 * 1000
        samples = _dense_span(span_48h, 0.50, start_ms=0)
        # "Now" is 10 days after the persisted data, well past the 7d trim.
        now_ms = samples[-1][0] + 10 * 24 * 60 * 60 * 1000

        rm.load_vol_history("BTC-PERP", samples, now_ms)

        assert rm._vol_history["BTC-PERP"] == []
        assert rm._vol_history_sufficient("BTC-PERP") is False


# ---------------------------------------------------------------------------
# TestBreakoutDetection
# ---------------------------------------------------------------------------

class TestBreakoutDetection:
    """Breakout detection triggers (section 6.3)."""

    def test_distance_breakout(self):
        rm = _rm()
        _seed_vol_history(rm)
        cfg = _cfg(breakout_atr_distance=4.5)
        result = rm._check_breakout(
            mid_price=52500.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(), config=cfg,
        )
        assert result is not None
        assert result.action == RiskAction.CANCEL_AND_FLATTEN
        assert result.details["type"] == "distance"

    def test_5m_return_breakout(self):
        rm = _rm()
        _seed_vol_history(rm)
        cfg = _cfg()
        # threshold = 2.0 * 500 / 50000 = 0.02
        result = rm._check_breakout(
            mid_price=50000.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(rolling_return_5m=0.025),
            config=cfg,
        )
        assert result is not None
        assert result.action == RiskAction.CANCEL_AND_FLATTEN
        assert result.details["type"] == "return_5m"

    def test_vol_spike_breakout(self):
        rm = _rm()
        _seed_vol_history(rm, vol_low=0.30, vol_high=0.70)
        cfg = _cfg(vol_kill_percentile=0.95)
        # Vol at 0.70 → 100th percentile, above 95th
        result = rm._check_breakout(
            mid_price=50000.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(realized_vol=0.71),
            config=cfg,
        )
        assert result is not None
        assert result.action == RiskAction.CANCEL_AND_FLATTEN
        assert result.details["type"] == "vol_spike"

    def test_vol_spike_check_gated_by_bootstrap_window(self):
        """A single cold-start vol sample is trivially its own 100th
        percentile; the vol-spike check must not fire on it (same bootstrap
        gate as detect_regime / _check_volatility)."""
        rm = _rm()
        rm.record_vol("BTC-PERP", 0, 0.99)
        cfg = _cfg(vol_kill_percentile=0.95)
        result = rm._check_breakout(
            mid_price=50000.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(realized_vol=0.99),
            config=cfg,
        )
        assert result is None

    def test_no_breakout_when_within_bounds(self):
        rm = _rm()
        _seed_vol_history(rm)
        cfg = _cfg(breakout_atr_distance=4.5)
        result = rm._check_breakout(
            mid_price=50500.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(realized_vol=0.50, rolling_return_5m=0.001),
            config=cfg,
        )
        assert result is None

    def test_vol_spike_breakout_arms_recovery_timer(self):
        """A vol-spike breakout (fired from _check_breakout, which always
        runs before _check_volatility in evaluate()'s check order) must still
        arm the recovery timer, or the mandatory sustained-normalization wait
        (section 6.4) never starts once vol drops back below pause."""
        rm = _rm()
        _seed_vol_history(rm, vol_low=0.30, vol_high=0.70)
        cfg = _cfg(vol_kill_percentile=0.95)
        result = rm._check_breakout(
            mid_price=50000.0, anchor=50000.0, atr=500.0,
            vol_metrics=_vol(realized_vol=0.71),
            config=cfg,
        )
        assert result is not None
        assert result.details["type"] == "vol_spike"
        assert "BTC-PERP" in rm._vol_recovery_start

        # Vol now back below pause, _check_volatility must enforce recovery,
        # not resume immediately.
        result2 = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.05), cfg)
        assert result2 is not None
        assert result2.action == RiskAction.PAUSE_GRID


# ---------------------------------------------------------------------------
# TestVolatilityCircuitBreakers
# ---------------------------------------------------------------------------

class TestVolatilityCircuitBreakers:
    """Vol pause and kill thresholds (section 6.4)."""

    def test_pause_threshold(self):
        rm = _rm()
        _seed_vol_history(rm, vol_low=0.30, vol_high=0.70)
        cfg = _cfg(vol_pause_percentile=0.80, vol_kill_percentile=0.95)
        # Vol at ~85th percentile (above pause, below kill)
        result = rm._check_volatility(
            "BTC-PERP", _vol(realized_vol=0.64), cfg,
        )
        assert result is not None
        assert result.action == RiskAction.PAUSE_GRID

    def test_kill_threshold(self):
        rm = _rm()
        _seed_vol_history(rm, vol_low=0.30, vol_high=0.70)
        cfg = _cfg(vol_kill_percentile=0.95)
        # Vol at 100th percentile
        result = rm._check_volatility(
            "BTC-PERP", _vol(realized_vol=0.71), cfg,
        )
        assert result is not None
        assert result.action == RiskAction.CANCEL_AND_FLATTEN

    def test_no_kill_on_cold_start_single_sample(self):
        """A fresh bot's first-ever vol observation is trivially its own
        100th percentile, must not trip the kill switch (design doc 6.4
        defines these thresholds as percentiles of trailing 7d vol, which is
        meaningless with <48h of history; same bootstrap window already used
        by detect_regime)."""
        rm = _rm()
        rm.record_vol("BTC-PERP", 0, 0.99)
        cfg = _cfg(vol_kill_percentile=0.95)
        result = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.99), cfg)
        assert result is None

    def test_no_pause_with_thin_history(self):
        rm = _rm()
        rm.record_vol("BTC-PERP", 0, 0.99)
        cfg = _cfg(vol_pause_percentile=0.80)
        result = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.99), cfg)
        assert result is None

    def test_kill_fires_once_bootstrap_window_met(self):
        """Sanity check: the bootstrap gate only suppresses the check while
        history is thin, once 48h of history exists, kill still fires."""
        rm = _rm()
        _seed_vol_history(rm, n=50, vol_low=0.30, vol_high=0.70,
                           start_ms=0)  # spans a full 7d by default
        cfg = _cfg(vol_kill_percentile=0.95)
        result = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.71), cfg)
        assert result is not None
        assert result.action == RiskAction.CANCEL_AND_FLATTEN

    def test_recovery_timer_prevents_premature_resume(self):
        rm = _rm()
        last_ts = _seed_vol_history(rm, vol_low=0.30, vol_high=0.70)
        cfg = _cfg(vol_pause_percentile=0.80, vol_recovery_minutes=10.0)

        # First call: vol is high → PAUSE
        result1 = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.64), cfg)
        assert result1 is not None
        assert result1.action == RiskAction.PAUSE_GRID

        # Vol drops below pause, recovery timer starts, grid stays paused
        result2 = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.40), cfg)
        assert result2 is not None
        assert result2.action == RiskAction.PAUSE_GRID

        # 5 minutes later: still within recovery period
        rm.record_vol("BTC-PERP", last_ts + 5 * 60 * 1000, 0.40)
        result3 = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.40), cfg)
        assert result3 is not None
        assert result3.action == RiskAction.PAUSE_GRID

        # 11 minutes later: recovery period elapsed
        rm.record_vol("BTC-PERP", last_ts + 11 * 60 * 1000, 0.40)
        result4 = rm._check_volatility("BTC-PERP", _vol(realized_vol=0.40), cfg)
        assert result4 is None


# ---------------------------------------------------------------------------
# TestFundingTiers
# ---------------------------------------------------------------------------

class TestFundingTiers:
    """Two-tier funding response (section 6.5)."""

    def test_moderate_funding_no_position(self):
        rm = _rm()
        cfg = _cfg()
        result = rm._check_funding(0.50, None, cfg)
        assert result is not None
        assert result.action == RiskAction.SKEW_FUNDING

    def test_extreme_funding_wrong_side_position(self):
        rm = _rm()
        cfg = _cfg()
        # Positive funding (longs pay), long position → wrong side
        result = rm._check_funding(1.50, _pos(size=0.5), cfg)
        assert result is not None
        assert result.action == RiskAction.PAUSE_GRID

    def test_extreme_funding_right_side_position(self):
        rm = _rm()
        cfg = _cfg()
        # Positive funding (longs pay), short position → right side (receiving)
        result = rm._check_funding(1.50, _pos(size=-0.5), cfg)
        assert result is not None
        assert result.action == RiskAction.SKEW_FUNDING  # Not pause

    def test_below_moderate(self):
        rm = _rm()
        cfg = _cfg()
        result = rm._check_funding(0.10, _pos(size=0.5), cfg)
        assert result is None

    def test_negative_extreme_funding_wrong_side(self):
        rm = _rm()
        cfg = _cfg()
        # Negative funding (shorts pay), short position → wrong side
        result = rm._check_funding(-1.50, _pos(size=-0.5), cfg)
        assert result is not None
        assert result.action == RiskAction.PAUSE_GRID


# ---------------------------------------------------------------------------
# TestInventoryZones
# ---------------------------------------------------------------------------

class TestInventoryZones:
    """Inventory cap classification (section 6.2)."""

    def test_hard_cap_returns_reduce_only(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0)
        result = rm._check_inventory(_pos(size=1.0), cfg)
        assert result is not None
        assert result.action == RiskAction.REDUCE_ONLY

    def test_soft_cap_returns_skew_inventory(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, soft_cap_pct=0.50)
        result = rm._check_inventory(_pos(size=0.6), cfg)
        assert result is not None
        assert result.action == RiskAction.SKEW_INVENTORY

    def test_normal_returns_none(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, soft_cap_pct=0.50)
        result = rm._check_inventory(_pos(size=0.3), cfg)
        assert result is None

    def test_zero_position_returns_none(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0)
        result = rm._check_inventory(_pos(size=0.0), cfg)
        assert result is None

    def test_none_position_returns_none(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0)
        result = rm._check_inventory(None, cfg)
        assert result is None

    def test_short_position_hard_cap(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0)
        result = rm._check_inventory(_pos(size=-1.0), cfg)
        assert result is not None
        assert result.action == RiskAction.REDUCE_ONLY


# ---------------------------------------------------------------------------
# TestDrawdownLimits
# ---------------------------------------------------------------------------

class TestDrawdownLimits:
    """Rolling drawdown checks (section 6.6)."""

    def test_daily_drawdown_breach_returns_kill(self):
        rm = _rm()
        base_ts = 1_000_000_000
        # Peak at 100k, then drop to 96.5k (3.5% drawdown > 3% limit)
        rm.record_equity(base_ts, 100000.0)
        rm.record_equity(base_ts + 60_000, 100000.0)
        rm.record_equity(base_ts + 120_000, 96500.0)

        result = rm._check_drawdown("BTC-PERP", 96500.0)
        assert result is not None
        assert result.action == RiskAction.KILL

    def test_weekly_drawdown_breach_returns_kill(self):
        rm = _rm()
        cfg = _cfg(max_daily_drawdown_pct=0.10, max_weekly_drawdown_pct=0.07)
        rm_custom = _rm(_bot_config(cfg))
        base_ts = 1_000_000_000
        # Peak 100k, current 92k → 8% drawdown > 7% weekly
        rm_custom.record_equity(base_ts, 100000.0)
        rm_custom.record_equity(base_ts + 24 * 60 * 60 * 1000, 95000.0)
        rm_custom.record_equity(base_ts + 48 * 60 * 60 * 1000, 92000.0)

        result = rm_custom._check_drawdown("BTC-PERP", 92000.0)
        assert result is not None
        assert result.action == RiskAction.KILL

    def test_drawdown_within_limits_returns_none(self):
        rm = _rm()
        base_ts = 1_000_000_000
        rm.record_equity(base_ts, 100000.0)
        rm.record_equity(base_ts + 60_000, 99000.0)

        result = rm._check_drawdown("BTC-PERP", 99000.0)
        assert result is None

    def test_rolling_window_excludes_old_entries(self):
        rm = _rm()
        base_ts = 1_000_000_000
        # Old peak (> 168h ago so it's outside BOTH windows)
        rm.record_equity(base_ts, 110000.0)
        # Recent entries within 24h, but > 168h after old peak
        recent_ts = base_ts + 169 * 60 * 60 * 1000  # 169h later
        rm.record_equity(recent_ts, 100000.0)
        rm.record_equity(recent_ts + 60_000, 98000.0)

        # 2% drawdown from 100k peak (not 110k which is outside both windows)
        result = rm._check_drawdown("BTC-PERP", 98000.0)
        assert result is None  # 2% < 3% daily limit

    def test_peak_equity_computed_within_window(self):
        rm = _rm()
        base_ts = 1_000_000_000
        rm.record_equity(base_ts, 95000.0)
        rm.record_equity(base_ts + 60_000, 100000.0)  # Peak
        rm.record_equity(base_ts + 120_000, 96000.0)

        result = rm._check_drawdown("BTC-PERP", 96000.0)
        # 4% drawdown from 100k peak > 3% limit
        assert result is not None
        assert result.action == RiskAction.KILL


# ---------------------------------------------------------------------------
# TestMomentumMicroFilter
# ---------------------------------------------------------------------------

class TestMomentumMicroFilter:
    """Momentum micro-filter (section 8.3)."""

    def test_triggered_by_large_5m_return(self):
        rm = _rm()
        mid = 50000.0
        atr = 500.0
        # threshold_5m = 1.2 * 500 / 50000 = 0.012
        result = rm._check_momentum(
            _vol(atr=atr, rolling_return_5m=0.015), mid,
        )
        assert result is not None
        assert result.action == RiskAction.SUPPRESS_NEW_ENTRIES
        assert result.details["trigger"] == "5m"

    def test_triggered_by_large_1m_return(self):
        rm = _rm()
        mid = 50000.0
        atr = 500.0
        # threshold_1m = 0.5 * 500 / 50000 = 0.005
        result = rm._check_momentum(
            _vol(atr=atr, rolling_return_1m=0.006), mid,
        )
        assert result is not None
        assert result.action == RiskAction.SUPPRESS_NEW_ENTRIES
        assert result.details["trigger"] == "1m"

    def test_not_triggered_when_both_small(self):
        rm = _rm()
        mid = 50000.0
        atr = 500.0
        result = rm._check_momentum(
            _vol(atr=atr, rolling_return_5m=0.001, rolling_return_1m=0.001), mid,
        )
        assert result is None


# ---------------------------------------------------------------------------
# TestErrorAndDesyncTracking
# ---------------------------------------------------------------------------

class TestErrorAndDesyncTracking:
    """Error and desync kill switches (section 6.6)."""

    def test_errors_at_threshold_return_kill(self):
        rm = _rm()
        for _ in range(10):
            rm.record_error()
        result = rm._check_errors()
        assert result is not None
        assert result.action == RiskAction.KILL

    def test_below_threshold_returns_none(self):
        rm = _rm()
        for _ in range(5):
            rm.record_error()
        result = rm._check_errors()
        assert result is None

    def test_maintenance_errors_dont_increment(self):
        rm = _rm()
        for _ in range(15):
            rm.record_error(is_maintenance=True)
        result = rm._check_errors()
        assert result is None

    def test_clear_errors_resets_counter(self):
        rm = _rm()
        for _ in range(9):
            rm.record_error()
        rm.clear_errors()
        for _ in range(5):
            rm.record_error()
        result = rm._check_errors()
        assert result is None

    def test_desync_at_threshold_returns_kill(self):
        rm = _rm()
        rm.record_desync(30_000)  # 30 seconds in ms
        result = rm._check_errors()
        assert result is not None
        assert result.action == RiskAction.KILL

    def test_desync_below_threshold_returns_none(self):
        rm = _rm()
        rm.record_desync(15_000)  # 15 seconds
        result = rm._check_errors()
        assert result is None

    def test_clear_desync(self):
        rm = _rm()
        rm.record_desync(30_000)
        rm.clear_desync()
        result = rm._check_errors()
        assert result is None


# ---------------------------------------------------------------------------
# TestPortfolioDelta
# ---------------------------------------------------------------------------

class TestPortfolioDelta:
    """Portfolio-level exposure cap (section 9.2)."""

    def test_same_side_positions_sum_and_breach(self):
        rm = _rm()
        # Both long, USD values add
        positions = {
            "BTC-PERP": _pos(size=0.5, avg_entry=50000.0),  # $25,000
            "ETH-PERP": Position(
                symbol="ETH-PERP", size=10.0, avg_entry_price=3000.0,
                unrealized_pnl=0.0,
            ),  # $30,000
        }
        # total_delta = $55,000
        # cap = 0.10 * 0.75 * 100000 = $7,500
        assert rm.check_portfolio_delta(positions, account_equity=100000.0) is True

    def test_opposite_side_positions_partially_cancel(self):
        rm = _rm()
        positions = {
            "BTC-PERP": _pos(size=0.1, avg_entry=50000.0),  # +$5,000
            "ETH-PERP": Position(
                symbol="ETH-PERP", size=-1.5, avg_entry_price=3000.0,
                unrealized_pnl=0.0,
            ),  # -$4,500
        }
        # total_delta = abs(5000 - 4500) = $500
        # cap = 0.10 * 0.75 * 100000 = $7,500
        assert rm.check_portfolio_delta(positions, account_equity=100000.0) is False

    def test_empty_positions_no_breach(self):
        rm = _rm()
        assert rm.check_portfolio_delta({}, account_equity=100000.0) is False

    def test_mark_price_uses_unrealized_pnl(self):
        rm = _rm()
        # avg_entry=50000, unrealized_pnl=500, size=0.1
        # mark_price = 50000 + 500/0.1 = 55000
        # delta = 0.1 * 55000 = $5,500
        positions = {
            "BTC-PERP": _pos(size=0.1, avg_entry=50000.0, upnl=500.0),
        }
        # cap = 0.10 * 0.75 * 100000 = $7,500
        assert rm.check_portfolio_delta(positions, account_equity=100000.0) is False

        # Larger unrealized PnL pushes over cap
        positions_big = {
            "BTC-PERP": _pos(size=0.5, avg_entry=50000.0, upnl=5000.0),
        }
        # mark = 50000 + 5000/0.5 = 60000, delta = 0.5 * 60000 = $30,000 > $7,500
        assert rm.check_portfolio_delta(positions_big, account_equity=100000.0) is True


# ---------------------------------------------------------------------------
# TestPreflightChecks
# ---------------------------------------------------------------------------

class TestPreflightChecks:
    """Pre-flight validation (section 6.1)."""

    def test_safe_config_returns_empty(self):
        rm = _rm()
        cfg = _cfg(
            leverage=2.0, levels_per_side=5, grid_step_bps_max=25.0,
            max_abs_position=0.5, max_flatten_slippage_bps=50.0,
            capital_allocation=0.10, max_daily_drawdown_pct=0.10,
        )
        violations = rm.preflight_check(cfg, 100000.0)
        assert violations == []

    def test_insufficient_liq_buffer(self):
        rm = _rm()
        # leverage=10 → liq_distance=0.10, grid_range=25*25/10000=0.0625, required=0.125
        cfg = _cfg(leverage=10.0, levels_per_side=25, grid_step_bps_max=25.0)
        violations = rm.preflight_check(cfg, 100000.0)
        assert any("liq buffer" in v for v in violations)

    def test_worst_case_loss_exceeding_drawdown(self):
        # Full risk budget on this one asset isolates the worst-case-loss
        # check from the liq-buffer check (leverage=3.0 alone clears that one).
        cfg = _cfg(
            leverage=3.0, levels_per_side=25, grid_step_bps_max=25.0,
            max_flatten_slippage_bps=50.0,
            capital_allocation=0.70, max_daily_drawdown_pct=0.03,
        )
        bot_cfg = BotConfig(
            assets=[cfg],
            portfolio=PortfolioConfig(total_risk_budget_pct=1.0, btc_weight=1.0),
            operational=OperationalConfig(max_consecutive_errors=10, max_time_desynced_seconds=30.0),
        )
        rm = _rm(bot_cfg)
        violations = rm.preflight_check(cfg, 100000.0)
        assert any("worst-case loss" in v for v in violations)

    def test_zero_equity(self):
        rm = _rm()
        violations = rm.preflight_check(_cfg(), 0.0)
        assert any("account_equity" in v for v in violations)

    def test_max_abs_position_derived_from_allocation(self):
        # exposure_frac = total_risk_budget_pct(0.10) * btc_weight(0.60)
        #                * capital_allocation(0.70) * leverage(2.0) = 0.084
        # max_abs_position = exposure_frac * equity / mid_price
        #                   = 0.084 * 100000 / 50000 = 0.168
        rm = _rm()
        cfg = _cfg(leverage=2.0, capital_allocation=0.70, max_daily_drawdown_pct=0.50)
        rm.preflight_check(cfg, 100000.0, mid_price=50000.0)
        assert cfg.max_abs_position == pytest.approx(0.168)

    def test_max_abs_position_untouched_without_mid_price(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, max_daily_drawdown_pct=0.50)
        rm.preflight_check(cfg, 100000.0)
        assert cfg.max_abs_position == 1.0


# ---------------------------------------------------------------------------
# TestFlattenabilityConstraint
# ---------------------------------------------------------------------------

class TestFlattenabilityConstraint:
    """Dynamic hard cap from flattenability (section 5.7)."""

    def test_normal_liquidity_equals_hard_cap(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, max_flatten_slippage_bps=50.0, depth_impact_scale=1.0)
        # spread=1 bps, depth=100 → max_flattenable = (50-1)/1 * 100 = 4900 >> 1.0
        effective = rm.compute_effective_hard_cap(cfg, 1.0, 100.0)
        assert effective == 1.0

    def test_thin_liquidity_tightens_cap(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, max_flatten_slippage_bps=50.0, depth_impact_scale=1.0)
        # spread=1, depth=0.01 → max_flattenable = (50-1)/1 * 0.01 = 0.49
        effective = rm.compute_effective_hard_cap(cfg, 1.0, 0.01)
        assert effective < 1.0
        assert abs(effective - 0.49) < 0.01

    def test_wide_spread_reduces_flattenable(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, max_flatten_slippage_bps=50.0, depth_impact_scale=1.0)
        # Use small depth so flattenable < hard_cap for both spreads
        # spread=10: remaining=40, flattenable=40*0.02=0.80
        # spread=30: remaining=20, flattenable=20*0.02=0.40
        effective_narrow = rm.compute_effective_hard_cap(cfg, 10.0, 0.02)
        effective_wide = rm.compute_effective_hard_cap(cfg, 30.0, 0.02)
        assert effective_wide < effective_narrow

    def test_spread_exceeds_slippage_budget(self):
        rm = _rm()
        cfg = _cfg(max_abs_position=1.0, max_flatten_slippage_bps=50.0, depth_impact_scale=1.0)
        # spread=60 > slippage budget=50 → 0
        effective = rm.compute_effective_hard_cap(cfg, 60.0, 100.0)
        assert effective == 0.0


# ---------------------------------------------------------------------------
# TestEvaluateMethod
# ---------------------------------------------------------------------------

class TestEvaluateMethod:
    """Main evaluate() orchestration (section 3.3 step 3)."""

    def test_returns_continue_when_all_pass(self):
        rm = _rm()
        _seed_vol_history(rm)
        state = _state()
        result = rm.evaluate(state)
        assert result.action == RiskAction.CONTINUE

    def test_drawdown_overrides_everything(self):
        rm = _rm()
        _seed_vol_history(rm)
        # Set up drawdown breach
        base_ts = 1_000_000_000
        rm.record_equity(base_ts, 100000.0)
        rm.record_equity(base_ts + 60_000, 96000.0)

        state = _state(account_equity=96000.0)
        result = rm.evaluate(state)
        assert result.action == RiskAction.KILL

    def test_errors_override_breakout(self):
        rm = _rm()
        _seed_vol_history(rm)
        for _ in range(10):
            rm.record_error()

        # Also set up breakout condition
        state = _state(mid_price=55000.0)  # Far from anchor
        result = rm.evaluate(state)
        assert result.action == RiskAction.KILL  # Errors, not breakout

    def test_breakout_returns_cancel_and_flatten(self):
        rm = _rm()
        _seed_vol_history(rm)
        # mid = 52500, anchor = 50000, distance = 2500, breakout = 4.5*500 = 2250
        state = _state(mid_price=52500.0)
        result = rm.evaluate(state)
        assert result.action == RiskAction.CANCEL_AND_FLATTEN

    def test_priority_ordering(self):
        """drawdown > errors > breakout > vol > funding > inventory > momentum"""
        rm = _rm()
        # Don't seed vol history, avoids vol recovery timer interference.
        # Vol checks return None when no percentile is available, which is fine
        # since we're testing funding vs inventory priority here.

        # Set up inventory issue (soft cap)
        state = _state(
            position=_pos(size=0.6),
            funding_rate=0.0,
        )
        result = rm.evaluate(state)
        assert result.action == RiskAction.SKEW_INVENTORY

        # Now add funding concern, it's higher priority
        state2 = _state(
            position=_pos(size=0.6),
            funding_rate=0.50,
        )
        result2 = rm.evaluate(state2)
        assert result2.action == RiskAction.SKEW_FUNDING

    def test_momentum_suppresses_new_entries(self):
        rm = _rm()
        _seed_vol_history(rm)
        state = _state(
            vol_metrics=_vol(
                atr=500.0,
                rolling_return_5m=0.015,  # > 1.2 * 500/50000 = 0.012
            ),
        )
        result = rm.evaluate(state)
        assert result.action == RiskAction.SUPPRESS_NEW_ENTRIES
