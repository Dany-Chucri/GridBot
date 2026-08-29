# Risk Model, Implementation Guide

> Implementation details for the risk model defined in [design.md](design.md) section 6.

## Evaluation Order

`RiskManager.evaluate()` checks constraints most-severe-first. The first triggered action wins:

1. **Drawdown limits** (6.6), KILL
2. **Consecutive errors / desync**, KILL
3. **Breakout detection** (6.3), CANCEL_AND_FLATTEN
4. **Vol kill threshold** (6.4), CANCEL_AND_FLATTEN
5. **Vol pause threshold** (6.4), PAUSE_GRID
6. **Extreme funding + wrong-side inventory** (6.5), PAUSE_GRID
7. **Hard inventory cap** (6.2), REDUCE_ONLY
8. **Moderate funding** (6.5), SKEW_FUNDING
9. **Soft inventory cap** (6.2), SKEW_INVENTORY
10. **Momentum micro-filter** (8.3), SUPPRESS_NEW_ENTRIES
11. All clear, CONTINUE

## Drawdown Measurement

- **Rolling windows**: 24h and 168h, not calendar-based. See [design.md](design.md) section 6.6 for the boundary exploit this prevents.
- **Includes unrealized PnL**: measured against exchange-reported equity.
- **Source**: exchange-reported PnL is authoritative per section 10.6.

## Flattenability Constraint

The effective hard cap adapts to current liquidity conditions:

```
max_flattenable = (max_flatten_slippage_bps - current_spread_bps) / depth_impact_scale * recent_avg_depth
effective_hard_cap = min(configured_hard_cap, max_flattenable)
```

This is recomputed every cycle. When liquidity thins, position limits automatically tighten. See [design.md](design.md) section 5.7.

## Regime Detection

Regime is per-asset, never coupled across BTC and ETH ([design.md](design.md) section 9.3).

RANGE requires unanimous agreement from all signals. Any dissent pushes toward the more conservative regime. See [design.md](design.md) section 8.2.

`RiskManager.detect_regime` records the deciding signal per asset (`regime_reason()`): `insufficient-vol-history`, `vol-percentile-unavailable`, `vol-above-pause`, `price-far-from-ma`, `breakout-cooldown`, or `all-signals-clear`. The Supervisor appends it in parentheses to each `regime transition` log line so a return to RANGE names its cause (e.g. a breakout cooldown expiring).

## Pre-Flight Checks

The bot refuses to start if any of these fail:
- Liquidation buffer insufficient for worst-case inventory
- Worst-case loss (max inventory breakout + flatten slippage) exceeds max daily drawdown
- Flattenability constraint not satisfiable with current depth

These are not overridable. See [design.md](design.md) section 6.1.
