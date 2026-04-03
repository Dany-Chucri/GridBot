# GridBot Implementation Plan

> **Status:** Active
> **Baseline:** All modules scaffolded with stubs, types and config implemented.
> **Reference:** `docs/design.md` (authoritative), `docs/architecture.md`, `docs/risk-model.md`

---

## Overview

Eight phases, ordered by dependency. Each phase produces a testable increment. Phases 1-2 are pure logic (no I/O) and can be developed and tested in isolation. Phases 3-6 introduce I/O and external dependencies. Phase 7 wires everything together. Phase 8 is integration validation.

```
Phase 0: Foundation              [DONE]
Phase 1: GridEngine              [DONE]
Phase 2: RiskManager             [pure logic, no I/O]
Phase 3: StateStore              [SQLite, no exchange]
Phase 4: MarketData              [WS + REST connectivity]
Phase 5: PnLMonitor              [analytics, cross-check]
Phase 6: OrderManager            [exchange order ops]
Phase 7: Supervisor              [orchestration, lifecycle]
Phase 8: Integration & Testnet   [end-to-end validation]
```

**Dependency graph:**

```
Phase 1 (GridEngine) ──────┐
                            ├──> Phase 7 (Supervisor)
Phase 2 (RiskManager) ─────┤
                            │
Phase 3 (StateStore) ───────┤
                            │
Phase 4 (MarketData) ───────┤
                            │
Phase 5 (PnLMonitor) ───────┤
                            │
Phase 6 (OrderManager) ─────┘
         │
         └── depends on Phase 4 (for flatten callbacks)
```

Phases 1-3 have zero cross-dependencies and can be developed in parallel. Phase 4-6 can also be developed in parallel (Phase 6 needs Phase 4 types but not a running instance). Phase 7 requires all prior phases. Phase 8 requires Phase 7.

---

## Phase 0: Foundation [DONE]

Completed. Types, enums, dataclasses, config loading, project scaffolding, and module stubs are in place.

**Delivered:**
- `gridbot/types.py` — All shared types
- `gridbot/config.py` — Config dataclasses, YAML loader, BTC/ETH defaults
- All module files with method signatures and docstrings
- `pyproject.toml` with dependencies and tooling
- Test scaffolding

---

## Phase 1: GridEngine [DONE]

**File:** `gridbot/grid_engine.py`
**Dependencies:** None (pure calculation, uses only types and config)
**Design doc:** Sections 5.1-5.7, 6.2, 7.1, 7.3, 7.5-7.6

GridEngine is the highest-value starting point: fully testable with no mocks, exercises the core strategy logic, and validates the design's math before any exchange integration.

### 1.1 Grid spacing formula [DONE]

**Method:** `compute_effective_step(vol_metrics) -> float`
**Design doc:** Section 5.4

Implement the fee+spread+slippage-aware step floor:

```
effective_min_step = max(
    ATR_based_step,
    2 * maker_fee + current_spread_bps + grid_slippage_buffer + safety_margin
)
```

Where `ATR_based_step` is derived from ATR relative to price, scaled to fall within `[grid_step_bps_min, grid_step_bps_max]`.

Subtasks:
- Implement `_compute_grid_slippage_buffer(vol_metrics)` — the dynamic slippage buffer from section 5.7 that scales with realized vol vs baseline.
- Clamp the final step to `[grid_step_bps_min, grid_step_bps_max]` from config.
- Use conservative constants: `maker_fee = 0.2 bps`, `safety_margin = 1.5 bps`.

**Tests:**
- Step equals ATR-based value when frictions are small.
- Step widens to friction floor when ATR-based step is below friction sum.
- Slippage buffer increases with elevated vol, stays at floor when vol is at baseline.
- Step clamped to `[min, max]` range.

### 1.2 Order sizing [DONE]

**Method:** `_compute_order_size(vol_metrics, account_equity) -> float`
**Design doc:** Section 5.5

```
order_size = target_risk_per_level / realized_vol
order_size = clamp(order_size, min_size, max_size)
```

`target_risk_per_level` is derived from the asset's capital allocation, the number of levels, and account equity.

Subtasks:
- Derive `target_risk_per_level` from `capital_allocation * account_equity / levels_per_side`.
- Apply the vol inverse scaling.
- Clamp to `[min_size, max_size]`. Use placeholder constants for `min_size` (BTC: 0.001, ETH: 0.01) — these will be validated against testnet in Phase 8. Set `max_size` as a config-driven fraction of `max_abs_position / levels_per_side`.

**Tests:**
- Size decreases when vol increases (inverse relationship).
- Size is clamped at min/max bounds.
- Zero or near-zero vol doesn't produce infinity (guard against division by near-zero).

### 1.3 Inventory classification and skewing [DONE]

**Methods:** `_classify_inventory_zone(position_size) -> InventoryZone`, `_apply_inventory_skew(levels, position_size, zone) -> list[GridLevel]`
**Design doc:** Section 6.2

Zone classification:
- `NORMAL`: `abs(pos) < soft_cap`
- `SOFT_CAP`: `soft_cap <= abs(pos) < hard_cap`
- `HARD_CAP`: `abs(pos) >= hard_cap`

Skew behavior:
- `NORMAL` — symmetric, no modification.
- `SOFT_CAP` — reduce size on exposure-increasing side by a factor proportional to `(abs(pos) - soft_cap) / (hard_cap - soft_cap)`. Increase unwind side by the same factor. Determine "exposure-increasing side" from the sign of position.
- `HARD_CAP` — remove all levels on the exposure-increasing side. Mark remaining unwind levels as `reduce_only=True`.

**Tests:**
- Zone boundaries are correct at exact threshold values.
- NORMAL: levels unchanged.
- SOFT_CAP with long position: buy sizes reduced, sell sizes increased.
- HARD_CAP with long position: all buy levels removed, sell levels have `reduce_only=True`.
- Symmetric behavior for short positions.

### 1.4 Core grid level computation [DONE]

**Method:** `_compute_core_levels(anchor, step_bps, vol_metrics, inventory_zone, position_size) -> list[GridLevel]`
**Design doc:** Section 5.2 (Layer 1)

Generate `levels_per_side` buy levels below anchor and `levels_per_side` sell levels above anchor. Each level is spaced by `step_bps` basis points from the previous.

Subtasks:
- Generate price levels: `anchor * (1 - i * step_bps / 10000)` for buys, `anchor * (1 + i * step_bps / 10000)` for sells, `i` from 1 to `levels_per_side`.
- Clamp levels to within `+/- 2.5 ATR` of anchor (core range). Discard levels that fall outside.
- Assign `GridLayer.CORE` to all.
- Apply inventory skewing via `_apply_inventory_skew`.

**Tests:**
- Correct number of levels generated per side.
- Levels are symmetric around anchor.
- No levels outside `+/- 2.5 ATR` range.
- Buy levels below anchor, sell levels above.
- Levels correctly spaced by step_bps.

### 1.5 Expansion grid level computation [DONE]

**Method:** `_compute_expansion_levels(anchor, mid_price, step_bps, vol_metrics, inventory_zone, position_size) -> list[GridLevel]`
**Design doc:** Section 5.2 (Layer 2)

Active when `abs(mid - anchor) > core_range AND abs(mid - anchor) < breakout_threshold`. Uses `expansion_levels_per_side` levels at `step_bps * expansion_step_mult` spacing, within `+/- expansion_range_atr * ATR`.

**Important distinction:** The activation upper bound (`breakout_atr_distance`: BTC 4.5, ETH 4.0 ATR) and the expansion level range (`expansion_range_atr`: BTC 4.0, ETH 3.5 ATR) are different config values. Levels are generated out to `expansion_range_atr`, but the activation condition uses `breakout_atr_distance`. The gap between them (0.5 ATR) is a buffer zone where the expansion grid is active but no levels are placed — this allows mean-reversion at extreme levels before the breakout flattener fires (design doc section 6.3).

Subtasks:
- Check activation condition: mid price beyond core range but within breakout distance (`breakout_atr_distance * ATR`).
- Generate levels from the core range boundary out to `expansion_range_atr * ATR` (not `breakout_atr_distance`).
- Use wider step: `step_bps * expansion_step_mult`.
- Size using `expansion_allocation` fraction of the asset's budget.
- Apply inventory skewing (shared caps with core).

**Tests:**
- Returns empty when mid price is within core range.
- Returns empty when mid price is beyond breakout distance.
- Levels start at core range boundary and extend outward.
- Step is `expansion_step_mult` times the core step.
- Returns empty when regime doesn't support it (handled by caller, but test the activation condition).

### 1.6 Staggered placement [DONE]

**Method:** `_apply_stagger(levels, mid_price, placed_count) -> list[GridLevel]`
**Design doc:** Section 5.3

Sort levels by distance from mid price. Return only the nearest `stagger_initial_levels` per side plus any levels that have been promoted (via fill-triggered promotion).

Subtasks:
- Sort levels by `abs(level.price - mid_price)`.
- Select nearest `stagger_initial_levels` per side (buy and sell separately).
- The `placed_count` parameter tracks how many additional levels have been promoted via fills. Include those beyond the initial set.
- Return the selected subset.

**Tests:**
- Only nearest N levels per side are included.
- Levels farther than N are excluded.
- Fill-triggered promotion (higher placed_count) includes additional levels.

### 1.7 Anchor management [DONE]

**Methods:** `should_reanchor(...)`, `compute_new_anchor(...)`
**Design doc:** Section 5.1

Four conditions must ALL be true:
1. `abs(mid - anchor) > anchor_shift_threshold * ATR`
2. `time_since_drift_started > anchor_delay`
3. `regime == RANGE`
4. `vol_stable == True` (realized vol stable or declining)

`compute_new_anchor` returns mid price as the new anchor (simple re-centering).

**Tests:**
- Returns `False` when any single condition fails.
- Returns `True` only when all four pass.
- Each condition tested in isolation (other three held true, one toggled).
- `compute_new_anchor` returns mid price.

### 1.8 Pending flips [DONE]

**Method:** `_include_pending_flips(desired, pending_flips, grid_config, inventory_zone) -> list[DesiredOrder]`
**Design doc:** Section 7.6

Convert pending flips into `DesiredOrder` objects and merge into the desired set. If at hard cap, flips on the exposure-increasing side get `reduce_only=True`.

Subtasks:
- Generate a `DesiredOrder` for each `PendingFlip` using `make_client_order_id` with a "flip" prefix or distinct identifier.
- Dedup against existing desired orders at the same price/side (flip and grid level might target the same price — design doc section 5.3).
- Apply inventory cap rules: hard cap converts exposure-increasing flips to reduce-only.
- Merge into the desired list.

**Tests:**
- Pending flips appear in the output regardless of current anchor.
- Dedup when flip and grid level share the same price/side.
- Hard cap converts exposure-increasing flips to reduce-only.
- Empty pending flips list produces no changes.

### 1.9 Main entry point [DONE]

**Method:** `compute_desired_orders(state) -> list[DesiredOrder]`
**Design doc:** Sections 7.1, 3.3 step 5a

Orchestrates all sub-methods:
1. Return empty if `regime not in {RANGE}` (for core) or regime doesn't support expansion.
2. Compute effective step.
3. Compute order size.
4. Classify inventory zone.
5. Compute core levels (if RANGE).
6. Compute expansion levels (if activation condition met).
7. Apply staggered placement.
8. Convert `GridLevel` list to `DesiredOrder` list with deterministic IDs.
9. Include pending flips.

**Note:** GridEngine does not compute regime — it receives `state.regime` which is set by RiskManager (Phase 2). All Phase 1 tests must set `state.regime` explicitly (e.g., `state.regime = Regime.RANGE`) since the detection logic is not available yet.

**Tests:**
- Returns empty for TREND, HIGH_VOL, UNKNOWN regimes.
- Returns correct orders for RANGE with no inventory.
- Includes expansion levels when mid price is in expansion zone.
- Inventory skewing applied correctly end-to-end.
- Pending flips included.
- All orders have ALO time-in-force.
- All order IDs are deterministic (same inputs = same IDs).

### Phase 1 acceptance criteria

- All `NotImplementedError` removed from `grid_engine.py`.
- `pytest tests/test_grid_engine.py` passes with >90% coverage of grid_engine.py.
- No exchange calls, no I/O, no mocks needed.
- Grid math validated against hand-calculated examples from design doc parameters.

---

## Phase 2: RiskManager

**File:** `gridbot/risk_manager.py`
**Dependencies:** None (uses only types and config)
**Design doc:** Sections 6.1-6.8, 8.1-8.3, 9.2

Like GridEngine, RiskManager is pure logic — it evaluates state and returns decisions. No I/O.

### 2.1 Regime detection

**Method:** `detect_regime(symbol, mid_price, vol_metrics, moving_avg, last_breakout_ms, now_ms, config) -> Regime`
**Design doc:** Sections 8.1-8.2

RANGE requires ALL signals to agree:
1. Vol below `vol_pause_threshold` — requires percentile calculation against `_vol_history`.
2. Price within `X * ATR` of moving average (e.g., `2.0 * ATR`).
3. No breakout within `cooldown_minutes`.

If any signal dissents, return the more conservative regime (TREND or HIGH_VOL depending on which signal fired).

Subtasks:
- Maintain `_vol_history[symbol]` as a rolling window (target: 7 days of vol samples).
- Compute percentile of current vol within the history.
- Compare `abs(mid - moving_avg)` against an ATR-scaled threshold.
- Check cooldown timer against `last_breakout_ms`.
- Return `UNKNOWN` if history is below the minimum window (see bootstrap below).

#### Vol history bootstrap

On first deployment (or after DB wipe), the 7-day vol history is empty. The bot uses a **48-hour minimum window, expanding to 7 days**, with an explicit conservative bias during bootstrap:

- **< 48h of history:** Return `UNKNOWN`. The bot does not trade. This is the hard floor — percentiles from less than 48h of data are too noisy to act on.
- **48h–7d of history:** Compute percentiles over the available window. Apply a conservative bias: when the percentile result is ambiguous (i.e., close to a threshold boundary), resolve toward the more restrictive regime. Concretely, tighten the `vol_pause_percentile` threshold during bootstrap — e.g., use the 70th percentile instead of 80th as the pause trigger until the window reaches 7 days. This ensures the bot errs toward HIGH_VOL/pause rather than false RANGE during the noisy early window.
- **>= 7d of history:** Steady-state behavior. Use configured thresholds as-is.

The `_vol_history_sufficient(symbol) -> bool` helper exposes whether the minimum window has been met (used by Supervisor to log bootstrap status).

**Tests:**
- Returns RANGE when all signals agree.
- Returns TREND when price is far from moving average.
- Returns HIGH_VOL when vol exceeds pause percentile.
- Returns RANGE only after cooldown expires post-breakout.
- Returns UNKNOWN with < 48h of vol history.
- Bootstrap bias: same vol reading returns RANGE with 7d history but HIGH_VOL with 48h history (tighter threshold applies).
- Threshold tightening scales linearly from bootstrap to steady-state (at 48h the bias is maximal, at 7d it's zero).

### 2.2 Breakout detection

**Method:** `_check_breakout(mid_price, anchor, atr, vol_metrics, config) -> RiskDecision | None`
**Design doc:** Section 6.3

Breakout triggers on any of:
- `abs(mid - anchor) > breakout_atr_distance * ATR`
- `abs(return_5m) > return_threshold` (e.g., `2.0 * ATR / price`)
- Realized vol spike (current vol > `vol_kill_percentile`)

Returns `RiskDecision(CANCEL_AND_FLATTEN, ...)` if triggered, `None` if clear.

**Tests:**
- Triggers on distance breakout.
- Triggers on 5m return breakout.
- Triggers on vol spike.
- Does not trigger when all metrics are within bounds.

### 2.3 Volatility circuit breakers

**Method:** `_check_volatility(symbol, vol_metrics, config) -> RiskDecision | None`
**Design doc:** Section 6.4

Two thresholds:
- Pause: vol > `vol_pause_percentile` of trailing 7d → `PAUSE_GRID`
- Kill: vol > `vol_kill_percentile` of trailing 7d → `CANCEL_AND_FLATTEN`

Recovery: vol must stay below pause threshold for `vol_recovery_minutes` before clearing.

Subtasks:
- Track vol recovery timer per asset.
- Return `PAUSE_GRID` for pause-level, `CANCEL_AND_FLATTEN` for kill-level.
- Return `None` if vol is normal and recovery timer has elapsed.

**Tests:**
- Pause threshold returns PAUSE_GRID.
- Kill threshold returns CANCEL_AND_FLATTEN.
- Recovery timer prevents premature resume.

### 2.4 Funding rate checks

**Method:** `_check_funding(funding_rate, position, config) -> RiskDecision | None`
**Design doc:** Section 6.5

Two tiers:
- Moderate (`> funding_moderate_threshold`): `SKEW_FUNDING`.
- Extreme (`> funding_extreme_threshold`) AND inventory on paying side: `PAUSE_GRID`.

**Tests:**
- Moderate funding with no position: SKEW_FUNDING.
- Extreme funding with wrong-side position: PAUSE_GRID.
- Extreme funding with right-side position (receiving): SKEW_FUNDING (not pause).
- Below moderate: None.

### 2.5 Inventory cap enforcement

**Method:** `_check_inventory(position, config) -> RiskDecision | None`
**Design doc:** Section 6.2

- `abs(pos) >= hard_cap` → `REDUCE_ONLY`
- `soft_cap <= abs(pos) < hard_cap` → `SKEW_INVENTORY`
- Below soft cap → `None`

**Tests:**
- Hard cap returns REDUCE_ONLY.
- Soft cap returns SKEW_INVENTORY.
- Normal returns None.
- Zero position returns None.

### 2.6 Drawdown checks

**Method:** `_check_drawdown(symbol, current_equity) -> RiskDecision | None`
**Design doc:** Section 6.6

Rolling 24h and 168h windows. Drawdown = `(peak_equity - current_equity) / peak_equity` over the rolling window. Includes unrealized PnL.

**Data flow:** `current_equity` is always exchange-reported account equity, fetched by the Supervisor via REST each cycle (see Phase 7.5 step 1) and passed into `evaluate()`. RiskManager never fetches equity itself — the exchange is the sole source of truth for this value.

Subtasks:
- Maintain `_equity_history` as list of `(timestamp_ms, equity)` tuples. The Supervisor calls `record_equity(timestamp_ms, equity)` each cycle with the exchange-reported value.
- Compute rolling max equity within the 24h/168h window.
- Compare drawdown against `max_daily_drawdown_pct` / `max_weekly_drawdown_pct`.
- Return `KILL` if breached (dead state, manual restart required).

**Tests:**
- Daily drawdown breach returns KILL.
- Weekly drawdown breach returns KILL.
- Drawdown within limits returns None.
- Rolling window correctly excludes old entries.
- Peak equity is correctly computed within the window.

### 2.7 Momentum micro-filter

**Method:** `_check_momentum(vol_metrics) -> RiskDecision | None`
**Design doc:** Section 8.3

Suppress new entries if:
- `abs(rolling_return_5m) > 1.2 * ATR / mid_price` (approximate, ATR-relative)
- `abs(rolling_return_1m) > 0.5 * ATR / mid_price`

Returns `SUPPRESS_NEW_ENTRIES` if triggered.

**Tests:**
- Triggered by large 5m return.
- Triggered by large 1m return.
- Not triggered when both are small.

### 2.8 Error and desync tracking

**Method:** `_check_errors() -> RiskDecision | None`
**Design doc:** Section 6.6

- `consecutive_errors >= max_consecutive_errors` → `KILL`
- `time_desynced > max_time_desynced_seconds` → `KILL`

Maintenance errors do NOT count (already handled in `record_error`).

**Tests:**
- Errors at threshold return KILL.
- Below threshold returns None.
- Maintenance errors don't increment counter.
- `clear_errors` resets counter.

### 2.9 Portfolio delta cap

**Method:** `check_portfolio_delta(positions) -> bool`
**Design doc:** Section 9.2

```
portfolio_delta = abs(sum(pos.size * pos.mark_price for pos in positions))
portfolio_cap = total_risk_budget * portfolio_delta_mult * account_equity
```

Use **mark price** (not mid price or avg entry) for delta computation — mark price is the exchange's fair-value estimate and the basis for margin/liquidation calculations.

Returns `True` if cap breached. The Supervisor uses this to tighten individual caps.

**Tests:**
- Same-side positions (both long) sum and can breach cap.
- Opposite-side positions partially cancel.
- Empty positions: no breach.

### 2.10 Pre-flight checks

**Method:** `preflight_check(config, account_equity) -> list[str]`
**Design doc:** Section 6.1

Validates before startup:
1. Liquidation buffer: `liq_distance >= grid_range * liq_buffer_mult (2-3x)`.
2. Worst-case loss under max inventory does not exceed `max_daily_drawdown`.
3. Flattenability constraint can be satisfied with current depth estimates.

Returns a list of violation strings. Empty list = pass.

**Tests:**
- Safe config returns empty list.
- Insufficient liq buffer returns violation.
- Worst-case loss exceeding drawdown limit returns violation.

### 2.11 Flattenability constraint

**Method:** `compute_effective_hard_cap(config, current_spread_bps, recent_avg_depth) -> float`
**Design doc:** Section 5.7

```
max_flattenable = (max_flatten_slippage - spread) / depth_impact_scale * depth
effective_hard_cap = min(hard_cap, max_flattenable)
```

**Tests:**
- Normal liquidity: effective cap equals configured hard cap.
- Thin liquidity: effective cap tightens to flattenable limit.
- Wide spread reduces flattenable position.
- Negative remaining budget (spread > slippage limit): effective cap goes to zero or minimum.

### 2.12 Main evaluate method

**Method:** `evaluate(state) -> RiskDecision`
**Design doc:** Section 3.3 step 3

Runs all checks in severity order and returns the most restrictive:
1. `_check_drawdown`
2. `_check_errors`
3. `_check_breakout`
4. `_check_volatility`
5. `_check_funding`
6. `_check_inventory`
7. `_check_momentum`

First non-None result wins (most severe check runs first).

**Tests:**
- Returns KILL when drawdown breached (overrides everything).
- Returns CANCEL_AND_FLATTEN when breakout detected.
- Returns CONTINUE when all checks pass.
- Priority ordering: drawdown > errors > breakout > vol > funding > inventory > momentum.

### Phase 2 acceptance criteria

- All `NotImplementedError` removed from `risk_manager.py`.
- `pytest tests/test_risk_manager.py` passes with >90% coverage.
- No exchange calls, no I/O, no mocks needed.
- Each risk check independently testable with constructed inputs.

---

## Phase 3: StateStore

**File:** `gridbot/state_store.py`
**Dependencies:** None (uses only types and aiosqlite)
**Design doc:** Sections 4.4, 7.6

### 3.1 Schema design

Create tables:

```sql
schema_version    (version INTEGER PK, applied_ms)
grid_config       (symbol PK, anchor, range_atr, step_bps, epoch, config_hash, updated_ms)
positions         (symbol PK, size, avg_entry_price, unrealized_pnl, liq_price, updated_ms)
open_orders       (client_order_id PK, symbol, order_id, price, size, remaining, side, reduce_only)
fills             (fill_id PK, order_id, client_order_id, symbol, price, size, side, fee, timestamp_ms, is_maker)
regime            (symbol PK, regime TEXT, transition_ms)
pending_flips     (symbol, price, side, size, originating_fill_id, PRIMARY KEY (symbol, originating_fill_id))
bot_state         (symbol PK, state_json TEXT, updated_ms)
heartbeat         (symbol PK, timestamp_ms)
```

Subtasks:
- Implement `initialize()`: open aiosqlite connection, `CREATE TABLE IF NOT EXISTS` for all tables, `PRAGMA journal_mode=WAL` for crash safety.
- Ensure `data/` directory is created if missing.
- **Schema versioning:** The `schema_version` table tracks the current schema version. On `initialize()`, check the current version and apply any pending migrations sequentially. Migrations are simple Python functions (`_migrate_v1_to_v2`, etc.) that run `ALTER TABLE` / `CREATE TABLE` statements inside a transaction. Start at version 1. This avoids manual database wipes when the schema evolves between phases.

### 3.2 CRUD operations

Implement all load/save methods. Each save is a single transaction (`INSERT OR REPLACE`). Each load is a simple `SELECT`.

Methods to implement:
- `save_grid_config` / `load_grid_config`
- `save_position` / `load_position`
- `save_open_orders` / `load_open_orders` — bulk replace: delete all for symbol, insert batch.
- `record_fill` / `get_fills` — append-only. `get_fills` supports `since_ms` filter.
- `save_regime` / `load_regime`
- `save_pending_flips` / `load_pending_flips` — bulk replace per symbol.
- `save_bot_state` / `load_bot_state` — serialize `AssetState` to JSON.
- `update_heartbeat` / `get_last_heartbeat`

### 3.3 Close

Implement `close()` — commit any pending transaction, close connection.

**Tests (`tests/test_state_store.py` — new file):**
- Round-trip: save then load each data type, verify equality.
- Fill append: multiple fills, `get_fills` returns all; `since_ms` filter works.
- Bulk replace: save orders, save again with different set, load returns only latest set.
- Initialize creates tables (run against temp file).
- Concurrent save doesn't corrupt (WAL mode).

### Phase 3 acceptance criteria

- All `NotImplementedError` removed from `state_store.py`.
- Tests pass using a temp SQLite file (no mocks).
- All writes are transactional.
- Schema supports all data from design doc section 4.4.

---

## Phase 4: MarketData

**File:** `gridbot/market_data.py`
**Dependencies:** Hyperliquid SDK, websockets, aiohttp
**Design doc:** Sections 4.1-4.3

This is the first module that talks to external systems. Tests will need either the testnet or a mock server.

### 4.1 WS connection and price subscriptions

**Methods:** `connect()`, `disconnect()`

Subtasks:
- Establish WebSocket connection to `config.ws_url`.
- Subscribe to `l2Book` channel per asset (provides best bid/ask for spread computation and book depth). This is the primary source for bid, ask, and mid price. The `allMids` channel does **not** provide bid/ask — only mid prices — so it is insufficient for spread computation.
- Subscribe to `trades` channel per asset (for vol computation).
- Subscribe to `orderUpdates` for the user's account (requires wallet address).
- Implement reconnection with exponential backoff.
- Set `_ws_connected` flag. Track `_last_ws_message_ms`.
- **Fallback:** If `l2Book` subscription fails or data is stale, derive mid price from `allMids` and estimate spread from recent trade data or use a conservative default. This is a last resort — `l2Book` is the primary path.

### 4.2 Price update handling

**Method:** `_handle_price_update(symbol, bid, ask)`

Subtasks:
- Extract best bid and best ask from `l2Book` updates.
- Compute mid price: `(bid + ask) / 2`.
- Store `_mid_prices[symbol]`, `_best_bid[symbol]`, `_best_ask[symbol]`.
- Compute spread in bps: `(ask - bid) / mid * 10000`.

### 4.3 Trade stream processing

**Method:** `_handle_trade(symbol, price, size, timestamp_ms)`

Subtasks:
- Append trade price to `_return_buffers[symbol]` (deque with maxlen for rolling window).
- Aggregate into 1-minute candles in `_minute_candles[symbol]` (OHLCV, deque with maxlen for ~7 days).
- These buffers feed `compute_vol_metrics`.

### 4.4 Vol metrics computation

**Method:** `compute_vol_metrics(symbol) -> VolMetrics`
**Design doc:** Section 4.2

Subtasks:
- **Realized vol:** standard deviation of log returns from `_return_buffers`, annualized (`* sqrt(365 * 24 * 3600 / interval_seconds)`).
- **ATR proxy:** average true range from `_minute_candles` (high - low over N minutes, default N=14).
- **Spread bps:** `(best_ask - best_bid) / mid * 10000`.
- **Rolling returns:** 1m and 5m price changes from the return buffer.
- Handle insufficient data: return conservative defaults (high vol, wide spread) when buffer is thin. This causes the bot to be cautious on startup.

### 4.5 Order update handling (primary fill path)

**Method:** `_handle_order_update(raw) -> Fill | None`
**Design doc:** Section 4.3

Parse the WS `orderUpdates` message:
- Extract fill info: order ID, fill price, fill size, side, fee, timestamp.
- Determine if this is a full fill or partial fill.
- Construct and return a `Fill` object on full fill. On partial, update remaining quantity in local tracking.
- Acquire `_lock` before modifying shared state (serialization with REST path).

### 4.6 REST backup fetches

**Methods:** `fetch_open_orders(symbol)`, `fetch_position(symbol)`, `fetch_exchange_pnl(symbol)`, `fetch_book_depth(symbol, depth_bps)`

Subtasks:
- Use the Hyperliquid SDK's info endpoint to fetch open orders, position, and PnL.
- `fetch_book_depth`: fetch L2 order book, sum quantities within `depth_bps` of mid on the relevant side.
- Acquire `_lock` before modifying shared state from REST responses.
- Handle API errors with logging (don't crash on a single failed REST call).

### 4.7 Mark price

Either derive from WS (if available) or fetch via REST info endpoint. Store in `_mark_prices[symbol]`.

**Tests (`tests/test_market_data.py` — new file):**
- Vol computation: feed synthetic trade data, verify realized vol calculation against hand-computed value.
- ATR computation: feed synthetic candles, verify ATR.
- Spread computation: verify from bid/ask.
- Rolling returns: verify 1m/5m returns from known price sequence.
- Edge case: insufficient data returns conservative defaults.
- Integration test (testnet): connect, receive prices, disconnect cleanly. (Skip in CI, run manually.)

### Phase 4 acceptance criteria

- All `NotImplementedError` removed from `market_data.py`.
- Unit tests pass for derived metrics (vol, ATR, spread) using synthetic data.
- Manual testnet smoke test: connects, receives prices, computes metrics, disconnects cleanly.
- Reconnection logic handles dropped connections.

---

## Phase 5: PnLMonitor

**File:** `gridbot/pnl_monitor.py`
**Dependencies:** Types only (exchange data provided by callers)
**Design doc:** Sections 10.6, 6.6

### 5.1 Fill processing

**Method:** `record_fill(fill)`

Subtasks:
- Append fill to `_fills[symbol]` deque.
- Update `_realized_pnl[symbol]` by computing fill PnL. Track a running **average-cost** entry price per symbol (not FIFO) — this matches Hyperliquid's own PnL reporting and simplifies the exchange cross-check in 5.3.

The PnL computation for each fill (average-cost method):
- If fill increases position: update `avg_entry = (avg_entry * old_size + fill.price * fill.size) / new_size`. No realized PnL.
- If fill reduces position (sell when long, buy when short): `pnl = (fill.price - avg_entry) * fill.size` (adjusted for side). `avg_entry` stays unchanged.
- If fill flips position (e.g., short 5 → long 2): realize PnL on the closing portion, then set `avg_entry = fill.price` for the new direction.

### 5.2 Funding tracking

**Method:** `record_funding_payment(symbol, amount)`

Add to `_funding_payments[symbol]` running total.

### 5.3 Exchange cross-check

**Method:** `crosscheck(symbol, exchange_pnl, now_ms) -> bool`
**Design doc:** Section 10.6

Subtasks:
- Compare `_realized_pnl[symbol] + _funding_payments[symbol]` against `exchange_pnl`.
- If `abs(diff) > _divergence_threshold`: set `_pnl_diverged[symbol] = True`, log warning, return `True`.
- Rate-limit: only run if `now_ms - _last_crosscheck_ms[symbol] >= _crosscheck_interval_s * 1000`.

### 5.4 Total PnL computation

**Method:** `compute_total_pnl(symbol, position) -> float`

```
total = realized_pnl + unrealized_pnl + funding
```

If diverged, use exchange-reported unrealized.

**Tests (`tests/test_pnl_monitor.py` — new file):**
- Fill recording updates realized PnL correctly for long and short positions.
- Funding payments accumulate.
- Cross-check detects divergence above threshold.
- Cross-check returns False when within threshold.
- Total PnL sums all components.
- Rate limiting prevents too-frequent cross-checks.

### Phase 5 acceptance criteria

- All `NotImplementedError` removed from `pnl_monitor.py`.
- Tests pass with >90% coverage.
- No exchange calls (all data is passed in by callers).

---

## Phase 6: OrderManager

**File:** `gridbot/order_manager.py`
**Dependencies:** Hyperliquid SDK (exchange calls), MarketData (for flatten callbacks)
**Design doc:** Sections 7.1-7.6, 6.7-6.8

This is the most exchange-coupled module. It makes real orders.

### 6.1 SDK client initialization

**Method:** `initialize()`

Subtasks:
- Initialize the Hyperliquid Python SDK client with wallet/API key from environment or config.
- Validate connectivity with a simple info query (e.g., fetch account state).
- Store client reference.

### 6.2 Diff computation

**Method:** `_compute_diff(desired, current) -> (to_cancel, to_place)`
**Design doc:** Section 7.2

Subtasks:
- Build a lookup of current orders keyed by `client_order_id`.
- For each desired order: if a matching current order exists (same price, side, size, reduce_only), it's a no-op. Otherwise, it needs placement and the old order (if any) needs cancellation.
- Current orders with no matching desired order → cancel.
- This is a pure function — highly testable.

**Tests:**
- No diff when desired matches current.
- New order → to_place only.
- Stale order → to_cancel only.
- Changed size → cancel old, place new.
- Mixed scenario with cancels, places, and no-ops.

### 6.3 Batch submission

**Method:** `_submit_batch(cancels, placements)`
**Design doc:** Section 2.3

**Important:** Hyperliquid batch operations are **not transactional** — individual orders within a batch can fail independently (e.g., an ALO rejection does not roll back successful cancels in the same batch). The implementation must handle partially-applied batch state.

Subtasks:
- Build the batch request using the Hyperliquid SDK's batch order API.
- All cancels and placements in a single request.
- Parse the per-order response statuses. Each cancel and placement reports success/failure independently.
- On partial failure:
  - Track which cancels succeeded and which placements succeeded/failed.
  - Update local state to reflect the actual post-batch state (not the intended state).
  - Hand ALO rejections (BadAloPx) to `_handle_alo_rejection`.
  - For non-ALO placement failures: log the error, mark those desired orders as unplaced, and allow the next reconciliation cycle to retry them.
  - For cancel failures: log and flag for immediate REST reconciliation to determine actual order state.
- Log the batch: count of cancels, placements, successes, and failures.

### 6.4 ALO rejection handling

**Method:** `_handle_alo_rejection(order, mid_price, attempt) -> DesiredOrder | None`
**Design doc:** Section 7.4

Subtasks:
- Nudge price one tick farther from mid (for buys: lower; for sells: higher).
- Retry up to `post_only_max_retries`.
- Return adjusted order or `None` if exhausted.
- Track rejection count per cycle for alerting (>5/min → warning).

### 6.5 Fill handling and flip orders

**Method:** `compute_flip_order(fill, step_bps, inventory_zone_is_hard_cap) -> DesiredOrder | None`
**Design doc:** Section 7.5

Subtasks:
- Only flip on full fills (caller should not pass partial fills).
- Compute opposite-side price: buy fill → sell at `fill.price * (1 + step_bps / 10000)`, sell fill → buy at `fill.price * (1 - step_bps / 10000)`.
- If at hard cap: set `reduce_only=True`.
- Generate deterministic order ID for the flip.

**Tests:**
- Buy fill produces sell flip one step higher.
- Sell fill produces buy flip one step lower.
- Hard cap sets reduce_only.
- Correct client order ID.

### 6.6 Backstop stop-loss management

**Method:** `update_backstop(symbol, position, anchor, atr, breakout_atr_distance, backstop_buffer_atr)`
**Design doc:** Section 6.8

Subtasks:
- If position is zero: cancel any existing backstop, return.
- Compute trigger price: `anchor -/+ (breakout_atr_distance + backstop_buffer_atr) * ATR`.
- Build trigger order: `tpsl="sl"`, `isMarket=True`, `reduce_only=True`, full position size.
- Cancel existing backstop (by deterministic client order ID) and place new one in the same batch.
- Generate deterministic ID: `hash("backstop", symbol, direction, config_hash)`.

### 6.7 Emergency flatten protocol

**Method:** `execute_flatten(symbol, position, config, get_mid_price, get_book_depth, get_position) -> bool`
**Design doc:** Section 6.7

This is a state machine. Implement the full protocol:

1. **Pre-flatten depth assessment:** Call `get_book_depth` to assess liquidity. Determine if single IOC or chunked tranches.
2. **IOC with bounded slippage:** Compute `ioc_limit_price` from current mid + `max_flatten_slippage_bps`. Send via `_send_flatten_ioc` with `reduce_only=True`.
3. **Partial fill retry loop:** Loop while `remaining > min_size AND elapsed < flatten_time_budget`:
   - Refresh mid price via `get_mid_price`.
   - Recompute limit price.
   - Send IOC for `min(remaining, tranche_size)`.
   - Wait for fill confirmation (up to 2s).
   - Re-query position via `get_position` (always trust exchange).
   - If remaining > 0, pause `flatten_retry_pause_ms`.
4. **Slippage escalation:** If still remaining, double the slippage limit and try one final IOC.
5. **Dead state:** If still remaining after escalation, return `False` (caller enters DEAD state).

**Method:** `_send_flatten_ioc(symbol, side, size, limit_price)`
- Build IOC order with `reduce_only=True`.
- Submit as single order (not batch — flatten is urgent).

### 6.8 Cancel all

**Method:** `cancel_all_orders(symbol)`
- Fetch all open orders for symbol, submit batch cancel.

**Tests (`tests/test_order_manager.py`):**
- Diff computation (pure function, no mocks needed).
- Flip order computation (pure function).
- Integration tests for batch submission, backstop, and flatten require testnet or mock SDK.

### Phase 6 acceptance criteria

- All `NotImplementedError` removed from `order_manager.py`.
- Diff computation and flip order computation tested without mocks.
- Manual testnet smoke test: place orders, cancel orders, verify batch atomicity.
- Backstop placement verified on testnet.
- Flatten protocol tested on testnet with a small position.

---

## Phase 7: Supervisor

**File:** `gridbot/supervisor.py`
**Dependencies:** All other modules
**Design doc:** Sections 3.3, 4.4-4.5, 10.2-10.4

### 7.1 Module initialization

**Method:** `_initialize()`

Subtasks:
- Initialize StateStore (`await state_store.initialize()`).
- Initialize OrderManager (`await order_manager.initialize()`).
- Connect MarketData (`await market_data.connect()`).
- Wait for initial price data (first WS message) before proceeding.
- Set up structured logging (JSON format, section 10.2).

### 7.2 State recovery

**Method:** `_recover_state()`
**Design doc:** Section 4.4

Subtasks:
1. Load persisted state from StateStore for each asset.
2. Fetch open orders and position from exchange (REST) for each asset.
3. Reconcile:
   - Orders in local state but not on exchange → check fills endpoint, mark as filled or cancelled.
   - Orders on exchange with matching config hash → adopt into local state.
   - Unknown orders on exchange → cancel (orphans).
4. If persisted state shows `FLATTENING` and position is non-zero → resume flatten protocol.
5. Update `_asset_states` with reconciled state.

### 7.3 Pre-flight checks

**Method:** `_preflight_checks()`
**Design doc:** Section 6.1

Subtasks:
- Fetch account equity from exchange.
- For each asset, call `risk_manager.preflight_check(asset_config, equity)`.
- If any violations: log them, refuse to start (exit with non-zero code).
- This is a hard gate — no operator override allowed.

### 7.4 Main event loop

**Method:** `_main_loop()`
**Design doc:** Section 3.3

```python
while not self._shutdown_requested:
    for asset_config in self._config.assets:
        symbol = asset_config.symbol
        state = self._asset_states[symbol]

        if state.bot_state == BotState.DEAD:
            continue

        await self._run_cycle(symbol, asset_config)

    await asyncio.sleep(self._config.operational.cycle_interval_seconds)
```

**Note on timers:** The main loop tick (`cycle_interval_seconds`) and REST reconciliation interval (`reconcile_interval_seconds`) are separate concerns. The cycle tick drives grid recomputation and risk checks (should be short, ~1s). REST reconciliation is a backup consistency check (default: 5s) tracked by its own timer inside `_run_cycle` (see step 9 in 7.5). Do not conflate them — using `reconcile_interval_seconds` as the main loop sleep creates unnecessary latency for fill-driven grid updates.

Handle `BotState` transitions: RUNNING, COOLDOWN (wait for timer), FLATTENING (run flatten), MAINTENANCE (passive wait), DEAD (skip).

### 7.5 Cycle execution

**Method:** `_run_cycle(symbol, asset_config)`
**Design doc:** Section 3.3

Implement the 10-step data flow:

```python
# 1. Fetch exchange-reported equity (source of truth for drawdown checks)
account_equity = await self._market_data.fetch_account_equity()
self._risk_manager.record_equity(now_ms, account_equity)

# 2. Market data (WS-driven, but refresh metrics here)
vol_metrics = self._market_data.compute_vol_metrics(symbol)
mid_price = self._market_data.get_mid_price(symbol)
mark_price = self._market_data.get_mark_price(symbol)
state.mid_price = mid_price
state.mark_price = mark_price
state.vol_metrics = vol_metrics

# 3. Risk evaluation
regime = self._risk_manager.detect_regime(...)
state.regime = regime
decision = self._risk_manager.evaluate(state)

# 4. Handle risk action
if decision.action != RiskAction.CONTINUE:
    await self._handle_risk_action(symbol, decision.action, decision.reason)
    return

# 5. Grid computation and reconciliation
desired = self._grid_engines[symbol].compute_desired_orders(state)
current = state.open_orders
reanchored = self._grid_engines[symbol].did_reanchor()  # track if anchor shifted
await self._order_manager.reconcile(symbol, desired, current)

# 6. Update backstop stop-loss
#    Triggers on: position change (fill), anchor shift, or ATR change.
#    Design doc section 6.8: backstop must always track current anchor.
await self._order_manager.update_backstop(
    symbol, state.position, state.anchor, state.vol_metrics.atr,
    asset_config.breakout_atr_distance, asset_config.backstop_buffer_atr
)

# 7. Persist state
await self._state_store.save_bot_state(symbol, state)

# 8. PnL cross-check (on schedule)
if time_for_crosscheck:
    exchange_pnl = await self._market_data.fetch_exchange_pnl(symbol)
    self._pnl_monitor.crosscheck(symbol, exchange_pnl, now_ms)

# 9. REST reconciliation (on separate timer, not every cycle)
if time_for_rest_reconciliation:
    await self._rest_reconciliation(symbol)

# 10. Log cycle metrics
logger.info(...)
```

### 7.6 Risk action handling

**Method:** `_handle_risk_action(symbol, action, reason)`

Map each `RiskAction` to its operational response:

| Action | Response |
|---|---|
| `CONTINUE` | Proceed normally |
| `SKEW_INVENTORY` | Set inventory zone in state, GridEngine handles skewing |
| `REDUCE_ONLY` | Set inventory zone, GridEngine emits reduce-only orders |
| `SKEW_FUNDING` | Pass funding info to GridEngine for directional bias |
| `PAUSE_GRID` | Skip grid computation this cycle, existing orders remain |
| `SUPPRESS_NEW_ENTRIES` | Skip grid computation, existing orders remain |
| `CANCEL_AND_FLATTEN` | Cancel all orders, execute flatten if position exists, enter COOLDOWN |
| `KILL` | Cancel all orders, flatten, enter DEAD state, send critical alert |

### 7.7 REST reconciliation

**Method:** `_rest_reconciliation(symbol)`
**Design doc:** Section 4.3

Subtasks:
- Fetch open orders and position via REST.
- Compare against local state.
- On discrepancy: log warning, adopt exchange state, trigger grid recompute next cycle.
- Run on a timer (`reconcile_interval_seconds`), not every cycle if WS is healthy.

### 7.8 Graceful shutdown

**Method:** `_shutdown()`
**Design doc:** Section 4.5

1. Set all asset states to `SHUTTING_DOWN`.
2. Cancel all resting orders for all assets (batch cancel per asset).
3. Do NOT flatten.
4. Persist final state to StateStore.
5. Disconnect MarketData WS.
6. Close StateStore.
7. Log shutdown complete.

### 7.9 Signal handling

**In `main.py`:** Register `SIGTERM` and `SIGINT` handlers that call `supervisor.request_shutdown()`.

### 7.10 Maintenance detection

**Method:** `_handle_maintenance()`
**Design doc:** Section 10.4

Subtasks:
- Detect maintenance from WS disconnect + REST 503/connection-refused.
- Enter `BotState.MAINTENANCE`.
- Don't count errors toward kill switch.
- Exponential backoff on reconnection (start 1s, max 60s).
- On reconnect: full state reconciliation before resuming.

### 7.11 Alerting

**Method:** `_send_alert(severity, message)`
**Design doc:** Section 10.3

Start with logging-based alerts (structured log at appropriate level). Add Telegram/Discord webhook support as a follow-up. The alert interface should be pluggable (callback or strategy pattern) so the transport is decoupled.

### 7.12 Fill event processing

Wire up MarketData's fill detection to:
- `PnLMonitor.record_fill(fill)`.
- `OrderManager.compute_flip_order(fill, ...)` → add to desired set or pending flips.
- `OrderManager.update_backstop(...)` after position changes.
- `StateStore.record_fill(fill)`.

This requires an event callback or queue pattern. MarketData detects fills via WS; the Supervisor routes them to the appropriate handlers.

**Tests (`tests/test_supervisor.py` — new file):**
- Cycle execution with mocked modules: verify correct call order.
- Risk action routing: each action triggers the right response.
- Shutdown: verify cancel-but-no-flatten behavior.
- State recovery: verify reconciliation logic with mock exchange state.

### Phase 7 acceptance criteria

- All `NotImplementedError` removed from `supervisor.py` and `main.py`.
- Unit tests verify orchestration logic with mocked modules.
- Manual testnet smoke test: bot starts, connects, places grid, handles fills, shuts down cleanly.
- Signal handling works (SIGTERM/SIGINT → graceful shutdown).
- Structured logs include all required fields.

---

## Phase 8: Integration and Testnet

**Dependencies:** All phases complete
**Design doc:** Section 10.5

### 8.1 End-to-end integration tests

Create `tests/test_integration.py`:
- Full startup → cycle → shutdown sequence against testnet.
- Verify grid orders appear on exchange.
- Simulate fill (if testnet allows) and verify flip order.
- Verify backstop stop-loss placement and updates.
- Verify state persistence and recovery across restart.
- Verify PnL cross-check.

### 8.2 Failure mode testing

- Kill WS connection mid-cycle → verify REST reconciliation recovers.
- Simulate 503 responses → verify maintenance mode, no kill switch trip.
- Simulate drawdown breach → verify DEAD state.
- Simulate breakout → verify cancel + flatten + cooldown.
- Kill process → restart → verify state recovery and orphan cleanup.

### 8.3 Testnet soak preparation

**Design doc:** Section 10.5, Phase 1

Checklist:
- [ ] Deploy to VPS with systemd service file.
- [ ] Configure testnet credentials.
- [ ] Set minimum order sizes, single asset (BTC), reduced levels.
- [ ] Run for 48-72 hours continuously.
- [ ] Monitor: no kill switch triggers, no unrecoverable desync, no PnL divergence.
- [ ] Verify all log events fire (regime transitions, fills, reconciliation, etc.).
- [ ] Verify alerts work (at least log-based).

### 8.4 Post-soak analysis

- Review all fills: were they profitable after fees?
- Check grid spacing: was the fee floor binding, or was ATR-based step always larger?
- Check regime detection: did it correctly identify RANGE vs TREND periods?
- Check flatten behavior: did any flattens occur? Were they within slippage budget?
- Check backstop orders: were they maintained correctly?
- Tune `depth_impact_scale` from observed flatten behavior.

### Phase 8 acceptance criteria

- Bot runs on testnet for 48+ hours without manual intervention.
- No kill switch triggers from bot errors (maintenance-related pauses are acceptable).
- PnL cross-check never diverges beyond threshold.
- State survives at least one process restart with correct recovery.
- All risk mechanisms fire correctly when conditions are met.

---

## Implementation order summary

```
Start
  │
  ├── Phase 1: GridEngine ────────────────┐
  ├── Phase 2: RiskManager ───────────────┤  (parallel, no dependencies)
  ├── Phase 3: StateStore ────────────────┤
  │                                       │
  ├── Phase 4: MarketData ────────────────┤
  ├── Phase 5: PnLMonitor ────────────────┤  (parallel, minimal cross-deps)
  ├── Phase 6: OrderManager ──────────────┤
  │                                       │
  └── Phase 7: Supervisor ────────────────┘  (requires all above)
       │
       └── Phase 8: Integration & Testnet
```

Recommended serial path (if working alone):

```
Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7 → Phase 8
```

This front-loads the testable pure logic (Phases 1-2), then builds infrastructure (3-4), then wires in exchange operations (5-6), and finally orchestrates (7-8).
