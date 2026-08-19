# Hyperliquid Perpetuals Grid Bot — Design Document

> **Status:** Authoritative design reference
> **Assets:** BTC-PERP, ETH-PERP
> **Revision:** 1.3 — parameter consistency fixes: ETH expansion/breakout buffer, flattenability formula correction (see decision log 12.4, entries 20–21)

---

## Table of Contents

1. [Introduction & Design Philosophy](#1-introduction--design-philosophy)
2. [Hyperliquid Platform Constraints](#2-hyperliquid-platform-constraints)
3. [System Architecture](#3-system-architecture)
4. [Market Data & State Management](#4-market-data--state-management)
5. [Strategy: Grid Design](#5-strategy-grid-design)
6. [Risk Model](#6-risk-model)
7. [Order Lifecycle](#7-order-lifecycle)
8. [Regime Detection](#8-regime-detection)
9. [Multi-Asset Portfolio Management](#9-multi-asset-portfolio-management)
10. [Deployment & Operations](#10-deployment--operations)
11. [Default Parameters](#11-default-parameters)
12. [Decision Log](#12-decision-log)

---

## 1. Introduction & Design Philosophy

### 1.1 What This Bot Is

A Python-based grid trading bot for Hyperliquid perpetual futures. It harvests mean-reversion profit in sideways (choppy) markets by placing symmetric limit orders around a central price anchor. It runs autonomously with risk guards that protect capital during adverse regimes.

### 1.2 What This Bot Is Not

- Not a latency-sensitive market maker competing with professional HFT firms.
- Not a directional trading system. It has no opinion on trend direction.
- Not a set-and-forget system. It requires monitoring, parameter tuning, and periodic human oversight.

### 1.3 Design Principles

1. **Exchange is truth.** Local state is a convenience cache. All risk decisions trust exchange-reported positions, orders, and PnL. Local ledgers are for analytics only.

2. **Survive first, profit second.** Every design decision prioritizes capital preservation. The bot should lose opportunities before it loses money to adverse regimes.

3. **Minimal complexity for current needs.** No feature flags, backwards-compatibility shims, or abstractions for hypothetical futures. Code what is needed now. Add layers when backtest data justifies them.

4. **Idempotent operations.** Every reconciliation cycle must produce the same result regardless of how many times it runs. Duplicate orders, ghost orders, and half-deployed grids are unacceptable.

5. **Fail safe, not fail open.** When in doubt — unknown state, connectivity loss, unexpected errors — cancel orders and flatten. Never leave unmonitored exposure.

### 1.4 The Two Classic Grid Deaths

Every design choice in this document defends against one or both of these failure modes:

| Failure Mode | Cause | Consequence |
|---|---|---|
| **Trend breakout** | Price moves directionally beyond grid range | Runaway inventory accumulation, margin exhaustion, liquidation |
| **Fee/spread erosion** | Grid spacing too tight relative to trading costs | Every fill is a net loss; slow guaranteed bleed |

---

## 2. Hyperliquid Platform Constraints

Understanding the exchange's specific behaviors is prerequisite to any design decision.

### 2.1 Connectivity

| Channel | Endpoint (Mainnet) | Endpoint (Testnet) |
|---|---|---|
| WebSocket | `wss://api.hyperliquid.xyz/ws` | `wss://api.hyperliquid-testnet.xyz/ws` |
| REST (Info) | `https://api.hyperliquid.xyz/info` | `https://api.hyperliquid-testnet.xyz/info` |
| REST (Exchange) | `https://api.hyperliquid.xyz/exchange` | `https://api.hyperliquid-testnet.xyz/exchange` |

Use the official Python SDK (or a compatible client) for all exchange interactions.

### 2.2 Order Types & Flags

The bot relies on these order capabilities:

| Type / Flag | Usage |
|---|---|
| **GTC** (Good-Til-Cancel) | All resting grid orders. They persist until filled or explicitly cancelled. |
| **Post Only (ALO)** | All grid entry/exit orders. Guarantees maker execution — rejected if it would immediately match. This is how we enforce maker-only fees. |
| **IOC** (Immediate-Or-Cancel) | Emergency flatten only. Takes liquidity at market price. |
| **Reduce Only** | Unwind orders when at or beyond inventory hard cap. Exchange enforces that these cannot increase position size. |

### 2.3 Batch Order Operations

**Critical design requirement:** Hyperliquid supports batch order submission (bulk cancel + bulk place in a single request). All order operations must use batch calls.

**Reasoning:** Running two assets with up to 40 levels each means potentially 80+ order operations per reconciliation cycle. Individual REST calls would:
- Saturate per-wallet rate limits
- Create timing windows where the grid is partially deployed (some orders live, others pending)
- Increase latency proportionally to order count

Batch operations are atomic from the exchange's perspective. The full cancel-then-place diff is submitted as one request, eliminating partial-grid states.

### 2.4 Rate Limits & Open Order Limits

HL imposes per-user rate limits on API calls and a finite cap on open orders (base: 1000, scaling with volume up to 5000). The design must:
- Minimize API call count (batch operations, not individual calls)
- Track open order count across both assets to stay below limits
- Use staggered order placement (nearest levels first) to avoid large bursts
- Budget order slots for server-side backstop stop-losses: 2 slots (1 per asset, see section 6.8). These are always-on and must not be displaced by grid orders.

### 2.5 Exchange Maintenance & Downtime

HL has had scheduled and unscheduled maintenance windows. The bot must distinguish between transient errors (retry) and maintenance (wait passively).

**Maintenance-awareness mode:** When the exchange returns 503, connection-refused, or similar patterns:
- Enter a passive wait state
- Do **not** count these toward `max_consecutive_errors` (which triggers the kill switch)
- On reconnect, perform a full state reconciliation before resuming any trading

Without this, a routine 5-minute maintenance window could trip the kill switch and require manual restart.

---

## 3. System Architecture

### 3.1 Module Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Supervisor                              │
│  (main event loop, orchestration, alerting, shutdown handler)   │
└──────┬──────────┬───────────┬───────────┬───────────┬───────────┘
       │          │           │           │           │
  ┌────▼───┐ ┌───▼────┐ ┌───▼─────┐ ┌───▼─────┐ ┌───▼──────────┐
  │Market  │ │State   │ │Grid     │ │Order    │ │Risk          │
  │Data    │ │Store   │ │Engine   │ │Manager  │ │Manager       │
  │        │ │        │ │         │ │         │ │              │
  │- WS    │ │- SQLite│ │- Levels │ │- Batch  │ │- Inventory   │
  │  price │ │- Fills │ │- Anchor │ │  ops    │ │- Breakout    │
  │  feed  │ │- Grid  │ │- Regime │ │- Post-  │ │- Vol circuit │
  │- WS    │ │  spec  │ │  gating │ │  only   │ │- Funding     │
  │  fills │ │- State │ │- Layer  │ │  retry  │ │- Drawdown    │
  │- Vol   │ │  snap  │ │  mgmt   │ │- Recon  │ │- Correlation │
  │  calc  │ │        │ │         │ │         │ │              │
  └────────┘ └────────┘ └─────────┘ └─────────┘ └──────────────┘
                                         │
                                    ┌────▼─────────┐
                                    │PnL / Funding │
                                    │Monitor       │
                                    │              │
                                    │- Fill PnL    │
                                    │- Funding     │
                                    │  tracking    │
                                    │- Exchange    │
                                    │  PnL cross-  │
                                    │  check       │
                                    └──────────────┘
```

### 3.2 Module Responsibilities

**MarketData** — Maintains the real-time view of the market.
- Subscribes to WS mid/mark price and trade streams.
- Subscribes to WS `orderUpdates` channel for real-time fill notifications (see [4.3](#43-dual-path-state-updates-ws-primary-rest-backup)).
- Computes rolling returns, realized volatility, ATR proxy, and current bid-ask spread.
- Exposes latest mid price, mark price, spread, and volatility metrics to other modules.

**StateStore** — Persistent storage (SQLite for single-node; Postgres if multi-node needed later).
- Stores: bot config version, current anchor + range + step + levels, current regime + timestamps, last known position + average entry, order map (level_price → order_id → status → remaining_qty), fills ledger, last heartbeat times.
- All writes are transactional. On crash recovery, state is consistent up to the last committed transaction.

**GridEngine** — Pure calculation module with no side effects.
- Takes current anchor, range, step, regime, and inventory state as inputs.
- Outputs a "desired orders" set: list of (price, side, size, flags) tuples.
- Manages two grid layers (Core and Expansion) with independent parameters.
- Applies inventory-aware skewing to order sizes.

**OrderManager** — The only module that talks to the exchange for order operations.
- Computes diffs between desired orders and actual open orders.
- Submits all changes as a single batch request (cancels + placements).
- Handles Post-Only (ALO) rejections with bounded retry logic.
- Uses deterministic client order IDs for idempotency (see [7.3](#73-deterministic-client-order-ids)).

**RiskManager** — Enforces all safety constraints.
- Leverage and liquidation buffer checks.
- Inventory cap enforcement (soft cap → skew, hard cap → reduce-only).
- Breakout detection with cancel + flatten.
- Volatility circuit breakers (pause and kill thresholds).
- Funding rate monitoring (two-tier: skew at moderate, pause at extreme).
- Drawdown limits (daily, weekly).
- Portfolio-level exposure cap across all assets.

**PnL / Funding Monitor** — Analytics and funding tracking.
- Tracks realized PnL from fills (local ledger).
- Periodically cross-checks against exchange-reported PnL. If divergence exceeds threshold, logs alert and defers to exchange numbers for all risk decisions.
- Monitors funding rate and bias.

**Supervisor** — Orchestration and lifecycle.
- Runs the main event loop.
- Coordinates module execution order each cycle.
- Handles periodic REST reconciliation.
- Manages alerting and structured logging.
- Implements graceful shutdown on SIGTERM/SIGINT (see [4.5](#45-graceful-shutdown)).

### 3.3 Data Flow Per Cycle

```
1. MarketData updates price, vol, spread (from WS)
2. MarketData processes any new fills (from WS orderUpdates)
3. RiskManager evaluates regime, circuit breakers, funding, drawdown
4. IF risk check fails → cancel orders, flatten if needed, enter cooldown
5. IF risk check passes:
   a. GridEngine computes desired order set (accounting for active layer, inventory skew)
   b. OrderManager computes diff against current open orders
   c. OrderManager submits batch operation
6. StateStore persists updated state
7. PnL Monitor cross-checks with exchange (on schedule, not every cycle)
8. Supervisor logs cycle metrics
```

---

## 4. Market Data & State Management

### 4.1 Price Inputs

| Price Type | Source | Usage |
|---|---|---|
| **Mid price** | Derived from best bid/ask on WS | Grid placement logic, regime detection, anchor calculations |
| **Mark price** | From WS or info endpoint (if available) | Risk checks, liquidation buffer calculations |
| **Bid-ask spread** | Derived from best bid/ask | Grid step floor calculation (see [5.4](#54-grid-spacing-fee--spread--slippage-aware)) |

If the SDK exposes both mid and mark, store both. If only one is available, use mid for signals and maintain larger safety buffers on risk calculations.

### 4.2 Derived Metrics

Computed continuously from the price stream:

- **Rolling returns** (1m, 5m windows) — used by momentum micro-filter and breakout detection.
- **Realized volatility** — standard deviation of high-frequency returns (1s or 5s intervals), annualized. Used for vol-scaled sizing and circuit breakers.
- **ATR proxy** — computed from minute-level candles (or synthetic candles from trade stream). Used for breakout thresholds, anchor shift detection, and grid step scaling.
- **Current spread (bps)** — live bid-ask spread. Used as a floor component in grid step calculation.

### 4.3 Dual-Path State Updates: WS Primary, REST Backup

**This is a critical architectural decision.**

The bot maintains two channels for order/position state:

**Primary path — WebSocket `orderUpdates`:**
- Subscribe to the user's order update channel.
- Every fill, partial fill, cancellation, or rejection arrives as a real-time event.
- On receiving a fill: immediately update local position, trigger grid flip logic, update PnL ledger.
- This path drives fill → flip latency down to milliseconds.

**Backup path — Periodic REST reconciliation:**
- Every `reconcile_interval` (default: 5 seconds), fetch open orders and position via REST.
- Compare REST snapshot against local state.
- If discrepancy found: log warning, adopt exchange state as truth, recompute desired grid.
- This catches anything the WS path missed (dropped frames, reconnect gaps, exchange-side cancellations).

**Why both paths are necessary:**

Using REST-only reconciliation (as in the original research) creates a 2–10 second blind spot. During that window, fills can occur that the bot doesn't know about. It might cancel an already-filled order, or place a duplicate because the fill event hasn't propagated. Inventory tracking drifts, and all downstream decisions (caps, regime, PnL) use wrong data.

Using WS-only creates fragility — a single dropped message or reconnect can silently desync state with no recovery mechanism.

The dual-path design gives real-time responsiveness with guaranteed eventual consistency.

**Implementation constraint:** Both paths update the same local state. They must be serialized (e.g., via an async queue or lock) to prevent race conditions between a WS fill event and a REST reconciliation snapshot arriving simultaneously.

### 4.4 Persistence & Restart Safety

#### What Must Be Persisted (to StateStore)

| Data | Purpose |
|---|---|
| Bot config version + all parameters | Detect config changes across restarts |
| Current anchor price, range, step, active levels | Rebuild grid without re-deriving |
| Current regime + regime transition timestamp | Avoid re-entering a regime prematurely |
| Last known position + average entry price | Sanity check against exchange on restart |
| Order map: level_price → order_id → status → remaining_qty | Identify own orders vs unknown orders |
| Fills ledger (all fills with timestamps, prices, sizes) | PnL calculation, analytics |
| Last heartbeat / last successful reconciliation time | Detect stale state on restart |
| Cooldown state (if active) + cooldown start time | Resume cooldown after restart |

#### Restart Behavior

On startup, the bot executes this sequence:

1. Load config from file/env.
2. Load persisted state from StateStore.
3. Query exchange for current open orders + position (REST).
4. **Reconcile persisted state against exchange state:**
   - Orders in local state but not on exchange → mark as filled or cancelled (check fills endpoint).
   - Orders on exchange but not in local state → classify:
     - If order ID matches current grid epoch/config → adopt into local state.
     - If order ID is unknown → cancel (these are orphans from a previous config or crash).
5. Rebuild desired grid based on current regime rules and exchange-confirmed position.
6. Resume the main event loop.

This prevents ghost orders (leftover from a crash) and doubled grids (placing new orders without knowing about existing ones).

### 4.5 Graceful Shutdown

On receiving SIGTERM or SIGINT (from systemd restart, deployment, manual stop):

1. Set a shutdown flag — the main loop stops after the current cycle completes.
2. Cancel all resting grid orders (batch cancel).
3. **Do not flatten** — the operator may want to keep the position and restart shortly. Flattening on every restart would incur unnecessary taker fees.
4. Persist final state to StateStore (position, order map showing all cancelled, regime, timestamps).
5. Close WS connections cleanly.
6. Exit with code 0.

**Reasoning for cancel-but-don't-flatten:** A graceful shutdown typically means a planned restart (deploy, config change). Flattening would take a market order, pay taker fees, and realize any unrealized loss. The restart sequence (section 4.4) handles resuming from a position. If the operator wants to flatten before stopping, they can trigger that explicitly.

**Hard kill (SIGKILL / crash):** The restart sequence handles this — it reconciles against exchange state and cleans up orphaned orders. Graceful shutdown simply makes restart faster and cleaner.

---

## 5. Strategy: Grid Design

### 5.1 Grid Type: Anchored Adaptive (with Regime-Confirmed Re-centering)

The bot uses an anchored adaptive grid: orders are placed symmetrically around a central anchor price, and the anchor can shift when market conditions justify it.

**Why not a static range grid:** A fixed range becomes stale as the market drifts. The bot either runs out of levels on one side (all filled, no more orders to catch mean reversion) or sits with unfilled orders on the other side (no activity, wasted capital). Adaptive re-anchoring keeps the grid centered on the current trading range.

**The danger of adaptive re-anchoring:** If the anchor chases price too aggressively, the bot trend-chases. Price breaks up → grid re-centers higher → price mean-reverts → bot bought high. This is the core problem the staggering design solves.

#### Anchor Re-centering Rules

Re-anchoring is **never** immediate. It requires all four conditions simultaneously:

```
re_anchor = (
    abs(mid_price - anchor) > anchor_shift_threshold    # Price has drifted meaningfully
    AND time_since_drift_started > anchor_delay          # Drift has persisted (not a spike)
    AND current_regime == RANGE                          # Market structure confirms range
    AND realized_vol is stable or declining              # Not re-anchoring into rising vol
)
```

| Parameter | Default | Reasoning |
|---|---|---|
| `anchor_shift_threshold` | 1.5 ATR | Must be material drift, not noise |
| `anchor_delay` | 30 minutes | Filters out short-lived spikes and wicks |

**Why all four conditions:** The original research used only a timer (conditions 1 and 2). Adding regime confirmation (condition 3) and vol stability (condition 4) prevents re-anchoring during the transition from a breakout back to range — exactly when false re-anchoring is most dangerous. Price may have drifted and stabilized in time terms, but if vol is still elevated or the regime filter reads TREND, re-anchoring would chase.

### 5.2 Two-Layer Grid Architecture

The bot manages two concurrent grid layers, not three. The original research proposed three layers (Core, Expansion, Recovery). The Recovery layer is deferred.

#### Layer 1 — Core Grid

The primary grid. Active only during confirmed RANGE regime.

| Parameter | Value |
|---|---|
| Range | +/- 2.5 ATR from anchor |
| Levels per side | 25 |
| Grid step | 0.15–0.25% (see spacing formula in 5.4) |
| Capital allocation | 70% of asset's risk budget |
| Activation | `regime == RANGE` |
| Deactivation | Breakout trigger fires, OR regime transitions to TREND/HIGH_VOL |

On deactivation: all Core Grid orders are cancelled immediately.

#### Layer 2 — Expansion Grid

A wider, sparser grid that activates when price drifts beyond the Core range but before breakout flattening.

| Parameter | Value |
|---|---|
| Range | +/- 4 ATR from anchor |
| Levels per side | 15 |
| Grid step | Wider (scaled up from Core step) |
| Capital allocation | 30% of asset's risk budget |
| Activation | `abs(mid_price - anchor) > core_range AND abs(mid_price - anchor) < breakout_threshold` |
| Deactivation | Price returns to Core range, OR breakout trigger fires |

**Purpose:** Harvests mean reversion on mild breakouts without re-anchoring the Core grid. The wider spacing prevents overtrading during expansion.

**Why not three layers:** The Recovery layer (very wide, tiny size, post-flatten probing) adds a third set of parameters, activation conditions, and inventory interactions. With no backtest data, this is speculative complexity. The two-layer design already covers the critical transition: Core for normal chop, Expansion for mild drift. After a full breakout+flatten, the bot enters cooldown and waits for regime to return to RANGE before restarting Core — this is simpler and equally safe. The Recovery layer can be added in a future version once the two-layer behavior is validated.

#### Layer Interaction Rules

- Both layers can be active simultaneously (Core handles near-anchor, Expansion handles wider range).
- Inventory caps and position limits are **shared** — the combined position from both layers must respect the asset's caps.
- When the Expansion grid accumulates inventory and then the Core grid reactivates (price returns to range), the Core grid's sizing accounts for existing inventory from Expansion fills.

### 5.3 Staggered Order Placement

Instead of placing all levels at once:

1. Place the nearest 5 levels per side immediately.
2. Queue remaining levels.
3. Add queued levels progressively as fills occur or price moves toward them.

**Fill-triggered level promotion:** When a fill occurs on any placed level, the next queued level on that side is promoted and placed immediately (in addition to the flip order from section 7.5). This ensures continuous coverage as the grid fills inward. The promoted level and the flip order may target the same price — the deterministic order ID scheme (section 7.3) prevents duplication; reconciliation treats them as a single desired order.

**Benefits:**
- Avoids large order bursts that stress rate limits.
- Reduces open order count (important for HL's per-user limits, especially running two assets).
- Adapts liquidity deployment to where price actually trades.

### 5.4 Grid Spacing: Fee + Spread + Slippage Aware

A grid only has edge if the step size exceeds all frictions. The step floor is:

```
effective_min_step = max(
    ATR_based_step,
    2 * maker_fee + current_spread_bps + slippage_buffer + safety_margin
)

grid_step_bps >= effective_min_step
```

| Component | Description | Typical Value |
|---|---|---|
| `maker_fee` | Conservative maker fee estimate | ~0.2 bps (HL) |
| `current_spread_bps` | **Live** bid-ask spread, re-evaluated each cycle | Variable |
| `slippage_buffer` | Buffer for execution uncertainty | 1–2 bps |
| `safety_margin` | Additional margin for comfort | 1–2 bps |
| `ATR_based_step` | Volatility-scaled step for mean-reversion edge | ~10–25 bps |

**Why include live spread:** The original research used a static fee formula (`2*maker_fee + slippage_buffer + safety_margin`). But on HL, the bid-ask spread can widen significantly during volatility spikes or on less liquid pairs. If the spread exceeds your grid step, every fill is guaranteed to lose money — you buy at the ask and sell at the bid, both inside your grid step. Including the live spread as a floor component means the grid automatically widens (or pauses) when spreads blow out.

**Dynamic behavior:** If `current_spread_bps` exceeds `ATR_based_step`, the grid step widens to accommodate. If the spread exceeds a hard threshold (e.g., 3x normal), the bot should pause new placements until spreads normalize — this is effectively a liquidity circuit breaker.

### 5.5 Order Sizing: Volatility-Scaled

Each level's order size is scaled inversely to realized volatility:

```
order_size = target_risk_per_level / realized_vol
order_size = clamp(order_size, min_size, max_size)
```

| Parameter | Description |
|---|---|
| `target_risk_per_level` | Percentage of account value risked per level fill |
| `realized_vol` | Current annualized realized volatility |
| `min_size` | Exchange minimum order size (hard floor) |
| `max_size` | Per-level cap to prevent oversized orders in low-vol regimes |

**Reasoning:** In high volatility, each fill carries more directional risk per dollar. Scaling down size keeps the dollar-risk-per-fill consistent. In low volatility, sizes scale up to maintain capital efficiency.

**Low-vol interaction with grid step:** In low-volatility regimes, the ATR-based grid step shrinks (toward the fee floor) while vol-scaled order size increases (toward `max_size`). The combination — tighter spacing with larger orders — means more levels fill in a smaller price range with larger positions. If volatility then spikes, the bot may already be near the inventory cap before the vol circuit breaker fires. The `max_size` clamp is the primary defense: it should be set conservatively based on risk tolerance, not exchange maximums. The fee floor on grid step (section 5.4) provides a secondary bound by preventing arbitrarily tight spacing.

### 5.6 Price Inputs for Grid Logic

- Use **mid price** (from best bid/ask) for: grid level placement, anchor calculations, regime detection.
- Use **mark price** for: liquidation buffer checks, margin calculations.

The distinction matters for perps. Mid price reflects current market microstructure. Mark price reflects the exchange's fair-value estimate and is what determines liquidation.

### 5.7 Dynamic Slippage Model

The static `slippage_buffer` (1–2 bps) from section 5.4 is a reasonable default for calm conditions but fails to capture two realities: (1) price movement between grid calculation and order placement scales with volatility, and (2) emergency flatten slippage is a function of position size, spread, and book depth — not a constant. This section replaces the static buffer with condition-aware estimates and introduces a position sizing constraint that prevents accumulating unflattenable positions.

#### Grid Order Slippage (Post-Only)

Grid orders are limit orders with zero *execution* slippage by definition — Post-Only guarantees maker fills. The `slippage_buffer` in the grid step formula (section 5.4) represents price movement between calculation and placement. This component should scale with short-term volatility:

```
grid_slippage_buffer = base_slippage_bps + (realized_vol / baseline_vol - 1) * vol_slippage_scale

grid_slippage_buffer = clamp(grid_slippage_buffer, min_slippage_bps, max_slippage_bps)
```

| Parameter | Default | Reasoning |
|---|---|---|
| `base_slippage_bps` | 1.5 | Normal-conditions floor |
| `baseline_vol` | Trailing 7d median vol | Reference point for "normal" |
| `vol_slippage_scale` | 2.0 | Sensitivity to vol increase |
| `min_slippage_bps` | 1.0 | Hard floor |
| `max_slippage_bps` | 10.0 | Cap — beyond this, circuit breakers should be firing |

This replaces the static `slippage_buffer` in the grid step floor formula (section 5.4). The formula becomes:

```
effective_min_step = max(
    ATR_based_step,
    2 * maker_fee + current_spread_bps + grid_slippage_buffer + safety_margin
)
```

#### Emergency Flatten Slippage (IOC)

Flatten slippage is a function of position size, current spread, and book depth:

```
estimated_flatten_slippage_bps = current_spread_bps + (position_size / recent_avg_depth) * depth_impact_scale
```

Where `recent_avg_depth` is the rolling average of visible book depth within 50 bps of mid (computed from periodic L2 snapshots or WS book data). `depth_impact_scale` is a calibration constant (default: 1.0, tuned during testnet soak).

This estimate feeds into:
- The emergency flatten protocol (section 6.7) as the basis for `max_flatten_slippage_bps`.
- The worst-case loss calculation (section 6.3) for honest risk budgeting.
- The flattenability constraint below.

#### Flattenability Constraint

The bot must never accumulate a position it cannot flatten within its slippage budget. Each cycle, after computing desired position limits:

```
max_flattenable_position = (max_flatten_slippage_bps - current_spread_bps) / depth_impact_scale * recent_avg_depth

effective_hard_cap = min(hard_cap, max_flattenable_position)
```

The `- current_spread_bps` term is required for consistency with the flatten slippage estimation formula above: spread is a fixed cost component of any flatten, so only the remainder of the slippage budget is available for position-dependent market impact. Omitting it would overstate the flattenable position by `current_spread_bps / depth_impact_scale * recent_avg_depth`.

If `max_flattenable_position < hard_cap`, log a warning and use the tighter limit. This dynamically reduces position limits when liquidity thins — exactly when the risk of being stuck in a position is highest.

**Why not a full market impact model:** A proper impact model requires order-level book data, hidden liquidity estimation, and calibration against historical fills. That is market-maker infrastructure. The linear approximation above captures the right direction (larger positions + thinner books = more slippage) without requiring sophistication the bot doesn't have. The `depth_impact_scale` parameter absorbs calibration error and should be tuned conservatively (overestimate impact) during testnet and early mainnet phases.

---

## 6. Risk Model

This is the make-or-break section. A grid without robust risk management is a leveraged position accumulator with extra steps.

### 6.1 Leverage & Liquidation Buffer

**Rules:**
- Use low leverage: 1x–3x for the grid engine.
- The liquidation price must be outside a worst-case wick zone:

```
liq_distance >= grid_range * liq_buffer_mult
```

Where `liq_buffer_mult` = 2–3x (i.e., liquidation price is 2–3x the grid range away from current price).

**Pre-flight check:** Before the bot starts, compute whether the chosen leverage + sizing + levels can maintain this buffer under worst-case inventory (all levels on one side filled). If not, refuse to start and log the constraint violation. Do not allow the operator to override this with a flag — the math either works or it doesn't.

### 6.2 Inventory Caps & Inventory-Aware Quoting

Two thresholds control inventory behavior:

```
soft_cap = 50% of max_abs_position
hard_cap = max_abs_position
```

**Behavior by zone:**

| Zone | Condition | Action |
|---|---|---|
| **Normal** | `abs(pos) < soft_cap` | Full grid, symmetric sizing |
| **Soft cap** | `soft_cap <= abs(pos) < hard_cap` | Skew the grid: reduce order sizes in the exposure-increasing direction, increase sizes on the unwind side. Still within per-order size limits. |
| **Hard cap** | `abs(pos) >= hard_cap` | Cancel all orders that would increase exposure. Only place **Reduce Only** orders to unwind. |

**Why Reduce Only matters:** HL enforces reduce-only semantics server-side. An order flagged reduce-only that would increase position is rejected. This is a safety net — even if the bot's local state is wrong about position size, the exchange prevents accidental exposure increase.

### 6.3 Breakout Detection & Flatten

When price exits the intended range, the bot must act decisively.

**Breakout range definition:** The breakout detector uses a **fixed ATR distance from anchor**, independent of which grid layers are currently active. This avoids ambiguity between Core range (±2.5 ATR) and Expansion range (±4 ATR), and ensures the breakout threshold doesn't shift when layers activate or deactivate.

```
breakout_distance = breakout_atr_distance * ATR    (default: 4.5 ATR from anchor)
```

The Expansion grid range (±4 ATR) must fit within this distance. The 0.5 ATR buffer between the Expansion outer edge and the breakout threshold provides a narrow window for mean-reversion at extreme levels before the safety mechanism fires.

**Breakout conditions (any triggers):**

```
breakout = (
    abs(mid_price - anchor) > breakout_distance
    OR abs(return_5m) > return_threshold
    OR realized_vol_spike detected
)
```

**Action sequence on breakout:**

1. **Immediately** cancel all resting grid orders (both layers, batch cancel).
2. **If inventory is significant** (position > threshold): execute the emergency flatten protocol (section 6.7). This protocol handles depth assessment, chunked execution, partial fills, and retry escalation.
3. **Enter cooldown** for `cooldown_minutes` (default: 30 min).
4. **After cooldown:** re-evaluate regime. Only restart the Core Grid if regime reads RANGE.

**This is the single most important safety mechanism.** Most retail grid bots omit breakout detection entirely. They accumulate inventory in a trend until margin is exhausted. The flatten-on-breakout design caps the worst-case loss to:

```
worst_case_loss = (grid_range * position_at_breakout) + estimated_flatten_slippage + taker_fees
```

Where `estimated_flatten_slippage` is computed dynamically from the slippage model (section 5.7), not assumed to be a small constant. This is quantifiable and bounded, unlike the unbounded loss of holding through a trend.

**Pre-flight validation:** Before the bot starts, the pre-flight check (section 6.1) must verify that `worst_case_loss` under maximum inventory does not exceed `max_daily_drawdown`. `max_abs_position` is derived (section 9.1), not operator-set, so the check cannot be cleared by lowering it directly — if it fails, reduce `capital_allocation`, `leverage`, or `btc_weight`/`eth_weight` (whichever share of the risk budget this asset draws) until the math works. This closes the loop between position sizing, slippage estimation, and drawdown limits — the bot cannot start in a configuration where a single breakout could breach its own safety limits.

### 6.4 Volatility Circuit Breakers

A rolling volatility metric (realized stdev of 1s/5s returns, or ATR proxy from minute candles) drives two thresholds:

| Threshold | Default | Action |
|---|---|---|
| `vol_pause_threshold` | Top 20th percentile of trailing 7d vol | Stop placing new orders. Existing orders remain. |
| `vol_kill_threshold` | Top 5th percentile (or absolute spike rule) | Cancel all orders + execute the emergency flatten protocol (section 6.7). Enter cooldown. |

**Restart condition:** Vol must normalize below `vol_pause_threshold` for a sustained period (default: 10 minutes) before the grid resumes.

### 6.5 Funding Rate Awareness (Two-Tier)

Perpetual funding rates can turn grid profits into a slow bleed if the bot carries inventory on the paying side.

**Two-tier approach:**

| Tier | Condition | Action |
|---|---|---|
| **Moderate funding** | Annualized rate > moderate_threshold (e.g., 30%) | **Skew** grid sizes: reduce orders on the side that would put the bot on the funding-paying side, increase orders on the earning side. This biases the bot toward the funded direction without fully pausing. |
| **Extreme funding** | Annualized rate > extreme_threshold (e.g., 100%) AND inventory is aligned with the paying side | **Pause** the grid entirely. Do not place new orders until funding normalizes. |

**Why not a hard filter for all funding levels:** The original research defaulted to pausing on any extreme funding. But "extreme" funding often coincides with crowd overextension, which produces the mean-reversion chop that grids profit from. A hard pause at moderate levels would keep the bot offline during some of its best setups. The two-tier approach captures the nuance: moderate funding is a signal to bias, not stop; only truly extreme funding with wrong-side inventory justifies a full pause.

**Tradeoff acknowledged:** Skewing introduces mild directional bias. This is intentional and bounded — the skew is proportional to funding magnitude and capped by inventory limits.

### 6.6 Drawdown & Sanity Limits

Absolute safety stops that override all other logic:

| Limit | Action on Breach |
|---|---|
| `max_daily_drawdown` (e.g., 3% of account) | Cancel all orders + execute emergency flatten protocol (section 6.7) + disable until manual restart |
| `max_weekly_drawdown` (e.g., 7% of account) | Same |
| `max_consecutive_errors` (e.g., 10, excluding maintenance errors) | Same |
| `max_time_desynced_seconds` (e.g., 30s — local state can't reconcile with exchange) | Same |

**Drawdown calculation basis:**

- **Includes both realized and unrealized PnL.** Ignoring unrealized PnL would leave the bot exposed to exactly the catastrophic loss these limits exist to prevent — large adverse positions accumulating without triggering the safety stop. The tradeoff is that a sharp wick can trip the limit, force a flatten, and then price reverses (causing an unnecessary realized loss). This is accepted: the wick scenario causes a small bounded loss; the no-unrealized alternative can cause account destruction.

- **Rolling window, not calendar-based.** Daily drawdown uses a rolling 24-hour window; weekly uses a rolling 168-hour window. Calendar-based windows (UTC midnight reset) create a boundary exploit: a 2.9% loss at 23:59 and 2.9% at 00:01 is 5.8% in 2 minutes without tripping a 3% daily limit. Rolling windows eliminate this.

- **Measurement:** The drawdown is measured as total PnL change (realized fills + change in unrealized position value + funding payments received/paid) relative to account equity at the start of the rolling window. The exchange-reported PnL (section 10.6) is the authoritative source for this calculation.

These are non-negotiable. When tripped, the bot enters a **dead state** that requires human intervention to restart. This prevents compounding losses during unforeseen conditions.

### 6.7 Emergency Flatten Protocol

Sections 6.3, 6.4, and 6.6 all reference "flatten the position." This section defines exactly how that works. The previous design said "flatten to zero using IOC/market orders" — a single sentence that assumes the IOC fills completely, the exchange is responsive, and slippage is negligible. All three assumptions fail during the exact scenarios that trigger a flatten.

**Flatten is a state machine, not a single order.** When any kill switch fires (breakout, vol kill, drawdown), the bot enters `FLATTENING` state. This state persists until position is zero or the attempt is abandoned with a critical alert.

#### Step 1 — Pre-Flatten Depth Assessment

Before sending the first IOC, assess available liquidity. Use the current order book (L2 snapshot from REST, or cached from WS book subscription):

```
available_depth = sum of book size within max_flatten_slippage_bps of mid price
```

- If `available_depth >= position_size`: send a single IOC for the full position.
- If `available_depth < position_size`: chunk the flatten into tranches of `available_depth * flatten_tranche_pct` (default: 0.8). Do not try to eat the entire visible book — there is hidden liquidity but also slippage acceleration beyond visible depth. Send tranches sequentially with a brief pause between them to allow the book to refill.

**If the L2 snapshot is unavailable** (WS disconnected, REST timeout): skip the depth check and proceed directly to Step 2 with the full position size. A blind IOC is better than no flatten attempt.

#### Step 2 — IOC with Bounded Slippage

Each IOC order uses a limit price to cap slippage, not a pure market order:

```
ioc_limit_price = mid_price * (1 - max_flatten_slippage_bps / 10000)   # for sells
ioc_limit_price = mid_price * (1 + max_flatten_slippage_bps / 10000)   # for buys
```

All flatten IOCs carry the `Reduce Only` flag — the exchange enforces that these cannot increase position size, providing a server-side safety net even if local state is wrong.

**Why IOC with limit, not pure market:** HL's trigger market orders have a built-in 10% slippage tolerance. For the bot's explicit flatten logic, we want tighter control. A limit-priced IOC fills everything available up to the limit and cancels the rest — giving both aggression and a price floor. The retry loop (Step 3) handles the unfilled remainder.

#### Step 3 — Partial Fill Retry Loop

```
flatten_start = now()
remaining = abs(position)

while remaining > min_order_size AND elapsed < flatten_time_budget:
    refresh mid_price from latest WS data (or REST fallback)
    compute ioc_limit_price from current mid
    send IOC reduce-only for min(remaining, tranche_size)
    wait for fill confirmation (up to 2 seconds)
    remaining = abs(exchange_reported_position)   # always re-query; don't trust local math

    if remaining > 0:
        log warning: "partial flatten, {remaining} remaining, retrying"
        pause flatten_retry_pause_ms for book refill

if remaining > 0:
    # Escalation: widen slippage limit to 2x
    escalated_slippage = max_flatten_slippage_bps * 2
    recompute ioc_limit_price with escalated_slippage
    send final IOC at escalated limit
    wait for fill (up to 2 seconds)
    remaining = abs(exchange_reported_position)

if remaining > 0:
    # Failed to flatten — CRITICAL alert, enter dead state
    alert CRITICAL: "flatten failed, residual position {remaining}, manual intervention required"
    enter DEAD state (no further trading, all orders already cancelled)
```

**Key behaviors:**
- Every iteration re-queries the exchange for the actual position. Never rely on local arithmetic — fills may have occurred from other paths (backstop trigger, manual intervention).
- The `mid_price` is refreshed each iteration because during a breakout, price is moving fast. Using a stale price for the IOC limit would guarantee non-fills.
- The escalation step doubles the slippage budget for one final attempt. If the book has gapped beyond 2x the normal budget, the situation requires human intervention — continued widening risks filling at catastrophic prices.

#### Parameters

| Parameter | Default (BTC) | Default (ETH) | Reasoning |
|---|---|---|---|
| `max_flatten_slippage_bps` | 50 | 75 | Wide enough to fill in stress; bounded enough to prevent absurd prices. ETH wider due to lower liquidity. |
| `flatten_time_budget_seconds` | 10 | 10 | If you can't flatten in 10 seconds, something is seriously wrong |
| `flatten_tranche_pct` | 0.8 | 0.8 | Don't try to eat 100% of visible depth |
| `flatten_retry_pause_ms` | 300 | 300 | Brief pause for book refill between tranches |

#### Interaction with FLATTENING State

While in `FLATTENING` state:
- No grid orders are placed (both layers suppressed).
- The main loop continues running (for price updates and state tracking) but skips grid computation.
- The flatten retry loop runs as a sub-procedure within the main cycle — it does not block the event loop indefinitely. If the time budget expires mid-cycle, the bot enters DEAD state and the cycle completes normally (with all trading suppressed).
- The `FLATTENING` state is persisted to StateStore. On restart, if the bot was in `FLATTENING`, it re-queries position and resumes the flatten protocol if position is non-zero.

### 6.8 Server-Side Stop-Loss Backstop (Dead-Man's Switch)

Every mechanism in sections 6.3–6.7 depends on the bot process being alive and the exchange API being reachable *from the bot*. If the bot crashes, the VPS goes down, or network connectivity is lost during a crisis, the position is unprotected. This section defines a last line of defense that executes server-side, independent of the bot.

**Mechanism:** Hyperliquid supports server-side trigger orders (TP/SL) that evaluate against mark price continuously and execute even if the client is disconnected. The bot maintains a **standing stop-loss trigger order** for each asset where it holds a position.

#### Trigger Order Specification

```python
order_type = {
    "trigger": {
        "triggerPx": backstop_trigger_price,
        "isMarket": True,      # trigger market — maximum fill probability
        "tpsl": "sl"           # stop-loss direction validation
    }
}
reduce_only = True              # exchange-enforced: cannot increase position
```

#### Lifecycle

1. **Placement:** Whenever the bot's net position changes (fill detected via WS or REST reconciliation), update the backstop:
   - **Direction:** opposite to current position (long position → sell stop; short → buy stop).
   - **Size:** full current position size.
   - **Trigger price:** set wider than the bot's own breakout threshold:
     ```
     backstop_trigger = anchor - (breakout_atr_distance + backstop_buffer_atr) * ATR   # for longs
     backstop_trigger = anchor + (breakout_atr_distance + backstop_buffer_atr) * ATR   # for shorts
     ```
   - The `backstop_buffer_atr` (default: 1.0 ATR) ensures the bot's own breakout flatten fires first under normal conditions. The exchange-side stop is a fallback for when the bot fails to act.

2. **Updates:** On every position change:
   - Cancel the existing backstop order.
   - Place a new one with updated size and trigger price.
   - Use deterministic client order ID: `hash("backstop", symbol, position_direction, grid_config_hash)` for idempotency.
   - This cancel-and-replace is included in the same batch operation as other order updates when possible, to minimize API calls.

3. **Removal:** When position reaches zero, cancel the backstop order. Do not leave orphaned triggers — an orphaned stop-loss on a zero position could open a new position if the reduce-only flag somehow fails (defense in depth: always clean up).

4. **On anchor shift:** Update the backstop trigger price to track the new anchor. The backstop must always be positioned relative to the *current* anchor, not a stale one.

5. **On restart:** The restart sequence (section 4.4) reconciles backstop orders like any other order. If a backstop exists on the exchange matching the current config, adopt it. If it's stale (wrong size or trigger price), cancel and replace.

#### Parameters

| Parameter | Default | Reasoning |
|---|---|---|
| `backstop_buffer_atr` | 1.0 | Gap between bot's breakout threshold and exchange stop — bot acts first normally |
| `backstop_order_type` | trigger market, reduce-only | Maximum fill probability when the bot has failed |

#### Order Slot Budget

Each backstop consumes 1 slot from HL's open order limit (base: 1000). With 2 assets, that is 2 slots — negligible. The open order count tracking (section 2.4) must include backstop orders in its budget.

#### Why Trigger Market

When the backstop fires, the situation is already catastrophic: the bot is dead AND price has blown through the breakout threshold AND continued another full ATR beyond that. Getting out at any price is more important than price precision. HL's built-in 10% slippage tolerance on trigger market orders is acceptable — this is a survival cost, not a trading cost.

#### Why Reduce Only

Prevents the backstop from accidentally opening a position in the opposite direction if the bot's state was stale when the order was placed. HL enforces reduce-only semantics server-side — a reduce-only order that would increase position is rejected. This is a critical safety property.

#### What This Does NOT Protect Against

- **Exchange-wide outages** where the matching engine itself is down. No client-side or server-side trigger mechanism can address this. The low leverage requirement (section 6.1) and liquidation buffer are the defenses for that scenario — they ensure the position survives extended downtime without liquidation.
- **Oracle manipulation** that moves the mark price without corresponding market moves. This is an exchange-level risk mitigated by HL's robust mark price index (median of multiple sources). The bot cannot defend against it.

---

## 7. Order Lifecycle

### 7.1 Desired Order Set Computation

Each reconciliation cycle, the GridEngine produces a set of intended orders:

- **Buy orders** at levels below the anchor.
- **Sell orders** at levels above the anchor.
- Each order carries flags:
  - `Post Only (ALO)` for all grid orders.
  - `GTC` time-in-force.
  - `Reduce Only` flag added only for unwind orders (position at or beyond hard cap).

The GridEngine applies inventory skewing (section 6.2) and staggered placement (section 5.3) before emitting the desired set.

### 7.2 Reconciliation: Diff and Batch

Every `reconcile_interval` (default: 5 seconds), the OrderManager:

1. Compares the desired order set against the current open orders (maintained by the WS primary path, validated by REST backup).
2. Computes the minimal diff:
   - **Cancel:** orders that exist on exchange but are not in the desired set (stale levels, regime change, anchor shift).
   - **Place:** orders in the desired set that don't exist on exchange.
   - **No-op:** orders that match (same price, side, size, flags).
3. Submits cancels + placements as a **single batch request**.

**Why minimal diff matters:** Re-placing an order that's already resting at the correct price/size wastes an API call, resets the order's queue priority, and risks a brief window without coverage at that level. Only touch orders that actually need to change.

### 7.3 Deterministic Client Order IDs

Every order is assigned a deterministic client order ID:

```
client_order_id = hash(symbol, level_price, side, grid_config_hash, epoch)
```

Where `grid_config_hash` is a hash of the current anchor price + range + step. This ensures:

- **Idempotency:** Restarting the bot and re-computing the desired set produces the same IDs. If an order already exists with that ID, the bot knows it's its own and doesn't duplicate.
- **Config isolation:** When the grid re-anchors (shifting level prices), the new config produces different IDs. Orders from the previous anchor are trivially identifiable and can be batch-cancelled before the new grid is placed. No ambiguity about which orders belong to which grid configuration.
- **Epoch separation:** The epoch counter increments on each re-anchor event, providing additional disambiguation if level prices coincidentally overlap between configurations.

**Why include `grid_config_hash`:** The original research used `(symbol, level_price, side, epoch)`. But during a re-anchor, old and new grids can have overlapping price levels with the same side. Without a config identifier in the order ID, the bot might incorrectly adopt an old order as belonging to the new grid. Including the config hash eliminates this ambiguity.

### 7.4 Post-Only (ALO) Rejection Handling

HL rejects a Post-Only order if it would immediately match (common "BadAloPx" error — the limit price crosses the current best bid/ask).

**Handling:**
1. If Post-Only rejected: nudge the limit price one tick farther from mid and retry.
2. Maximum retries: 3 per level per cycle.
3. If still rejected after max retries: skip that level this cycle. It will be retried next cycle.

**Why this happens:** The grid computes levels based on a price snapshot, but by the time the order reaches the exchange, the book may have moved. Levels very close to mid price are most susceptible.

### 7.5 Fill Handling & Grid Flip

When a level fully fills (detected via WS `orderUpdates`, primary path):

1. Update local position and remaining-quantity tracking.
2. **If within inventory caps:** immediately place the opposite-side order one step away (classic grid flip). This is the core profit mechanism — buy at level N, sell at level N+1.
3. **If at or beyond inventory cap:** do not flip. Instead, place a reduce-only order at a favorable price to unwind. The grid "pauses" on that side until inventory normalizes.

**Partial fills:** Update remaining quantity in state. The partially filled order remains on the book. Once fully filled, trigger the flip. Do not flip on partial fills — this would create an unhedged position at the new level while the original level is still partially open.

### 7.6 Pending Flips Across Re-Anchoring

When the grid re-anchors, the desired order set is recomputed from the new anchor. Flip orders placed from fills under the **old** anchor may not correspond to any level in the new desired set. Without explicit handling, reconciliation (section 7.2) would cancel these flip orders as "not in desired set," orphaning the underlying position.

**Solution: Pending flips set.**

The bot maintains a separate "pending flips" list: a set of `(price, side, size, originating_fill_id)` tuples representing flip orders that arose from actual fills. This list is:

- **Added to** whenever a fill triggers a flip (section 7.5).
- **Included** in the desired order set regardless of the current anchor or grid configuration. Reconciliation treats them as first-class desired orders.
- **Removed from** when the flip order itself fills (profit taken), or when the underlying position is unwound by other means (inventory skewing, breakout flatten, manual intervention).
- **Persisted** to StateStore (section 4.4) so they survive restarts.

**Why this matters:** Re-anchoring is infrequent (30-minute delay + multi-condition gate), but when it occurs, any fills from the old grid that haven't yet flipped represent expected profit. Cancelling their flip orders converts those positions into inventory-skew-managed unwinds at potentially worse prices. The pending flips set preserves the original profit target.

**Interaction with inventory caps:** Pending flip orders are still subject to inventory cap enforcement. If the bot reaches the hard cap, pending flips on the exposure-increasing side are converted to reduce-only orders at the same price. On the unwind side, they remain as-is since they already reduce exposure.

---

## 8. Regime Detection

The grid runs only when the market is range-like. Regime detection gates the entire strategy.

### 8.1 Regime States

```
REGIME = RANGE | TREND | HIGH_VOL | UNKNOWN
```

| Regime | Grid Behavior |
|---|---|
| **RANGE** | Full grid active (Core + Expansion as applicable) |
| **TREND** | All orders cancelled. If inventory exists, flatten. Enter cooldown. |
| **HIGH_VOL** | Orders cancelled (vol circuit breaker). Wait for normalization. |
| **UNKNOWN** | Treat as HIGH_VOL (fail safe — don't trade what you don't understand). |

### 8.2 Detection Signals

The regime filter uses multiple signals for robustness — no single indicator decides:

1. **Volatility level:** Below `vol_pause_threshold` → supports RANGE. Above → supports HIGH_VOL.
2. **Trend strength:** Price within X * ATR of a moving average (e.g., EMA-50) → supports RANGE. Outside → supports TREND.
3. **ADX-like proxy (optional):** Low ADX readings support RANGE.
4. **No recent breakout:** No breakout event in the last `cooldown_minutes` → supports RANGE.

The regime is RANGE only if all supporting signals agree. Any dissenting signal pushes toward the more conservative regime.

### 8.3 Momentum Micro-Filter

Even within a RANGE regime, sudden short-term moves should suppress new order placement:

```
disable_new_entries = (
    abs(price_change_5m) > 1.2 * ATR
    OR abs(price_change_1m) > 0.5 * ATR
)
```

When active:
- **Existing** grid orders remain on the book (they may capture the reversion).
- **No new** levels are added (prevents stacking orders in the direction of a sudden move).

This is a micro-level complement to the macro regime filter. The regime might correctly read RANGE on a 1-hour timeframe, but a sudden 1-minute spike warrants a brief pause in new placements.

---

## 9. Multi-Asset Portfolio Management

### 9.1 Shared Risk Budget

BTC and ETH grids run independently but share a single account's capital and margin. They must share a risk budget:

```
total_risk_budget = 10% of account equity (adjustable)
btc_allocation = total_risk_budget * btc_weight   (default: 0.6)
eth_allocation = total_risk_budget * eth_weight   (default: 0.4)
```

Each asset's inventory caps (`max_abs_position`, `soft_cap`) are derived from its allocation.

### 9.2 Portfolio-Level Exposure Cap

**Critical addition:** Individual asset caps are necessary but not sufficient. Because BTC and ETH are highly correlated (~80-90%), both grids can accumulate same-side inventory simultaneously, producing portfolio-level exposure greater than either cap alone.

**Solution: Portfolio delta cap.**

```
portfolio_delta = abs(btc_position_usd + eth_position_usd)
portfolio_cap = total_risk_budget * portfolio_delta_mult   (e.g., 0.75)
```

The portfolio cap is **stricter** than the sum of individual caps. Example:
- BTC individual cap: $10,000
- ETH individual cap: $6,000
- Sum of individual caps: $16,000
- **Portfolio cap: $12,000** (75% of sum)

When `portfolio_delta > portfolio_cap`: tighten both assets' soft caps proportionally until combined exposure is within budget. In practice, the asset with the larger same-side exposure gets tightened more.

**Why not measure correlation directly:** Measuring rolling correlation between two assets requires choosing a window (1h? 4h? 1d?), defining "strong," and deciding on response. The correlation is ~0.85 consistently — it's not a variable to track but a constant to design around. The portfolio delta cap handles it implicitly: if both assets trend the same direction, the combined delta grows and the cap kicks in, regardless of what the correlation number is.

### 9.3 Independent Regime Detection

Each asset has its own regime detection. BTC can be in RANGE while ETH is in TREND. This is correct — while correlated in large moves, they can have different microstructure characteristics. Do not couple their regimes.

---

## 10. Deployment & Operations

### 10.1 Infrastructure

- **Run on a VPS** geographically close to HL endpoints. Latency is not the primary edge, but stable sub-100ms connectivity reduces reconciliation drift and fill detection delay.
- **Process management:** Use `systemd` (Linux) or equivalent supervisor to auto-restart on crash. The restart sequence (section 4.4) handles state recovery.
- **Single instance per account.** Never run two bot instances on the same HL account — they will fight over orders and position.

### 10.2 Logging

Use structured logs (JSON or consistent key-value format). Every log line should include:

- Timestamp (UTC, millisecond precision)
- Module name
- Log level
- Relevant context (asset, regime, position, action taken)

Log at minimum:
- Every regime transition
- Every order batch submission (with order count, cancels, placements)
- Every fill (price, size, side, resulting position)
- Every risk event (cap hit, circuit breaker, breakout, funding pause)
- Every reconciliation discrepancy (local vs exchange state mismatch)
- Startup and shutdown events

### 10.3 Alerting

Alerts (via Telegram, Discord, or email) must fire on:

| Event | Severity |
|---|---|
| Kill switch triggered (drawdown, errors) | Critical |
| Flatten failed — residual position after time budget expired (section 6.7) | Critical |
| Server-side backstop triggered (section 6.8) — bot was unable to flatten, exchange stop fired | Critical |
| Breakout flatten executed | High |
| Flatten partial fill — retrying (section 6.7) | High |
| Flattenability constraint tightened hard cap (section 5.7) | High |
| Reconcile discrepancy detected | High |
| Position exceeds soft cap | Warning |
| Backstop order update failed — position may be unprotected (section 6.8) | Warning |
| Repeated Post-Only rejections (> 5/min) | Warning |
| WS disconnected / reconnecting | Warning |
| Exchange maintenance detected | Info |
| Regime transition | Info |

### 10.4 Exchange Maintenance Handling

When the exchange enters maintenance:

1. WS disconnects. REST calls return 503 or connection refused.
2. Bot enters **maintenance-awareness mode:**
   - All errors during this period do **not** count toward `max_consecutive_errors`.
   - No retry storms — use exponential backoff on reconnection attempts.
   - Log the event, send an Info alert.
3. On reconnect:
   - Perform full state reconciliation (section 4.4 restart behavior).
   - Verify all orders are in expected state (exchange may have cancelled orders during maintenance).
   - Resume normal operation only after successful reconciliation.

### 10.5 Testing Progression

| Phase | Duration | Parameters |
|---|---|---|
| **Testnet soak** | 48–72 hours minimum | Full parameter set, normal operation |
| **Mainnet tiny** | 1–2 weeks | Minimum order sizes, single asset (BTC), reduced levels |
| **Mainnet small** | 2–4 weeks | Small but meaningful sizes, both assets |
| **Mainnet target** | Ongoing | Full target parameters, gradual scale-up |

Each phase must pass without: kill switch triggers, unrecoverable state desync, or unexplained PnL divergence. Promote to the next phase only when the current phase is stable.

### 10.6 PnL Cross-Check with Exchange

The local fills ledger tracks PnL for granular per-level analytics. But risk decisions must not depend solely on local accounting.

**Schedule:** Every 60 seconds (configurable), fetch the exchange-reported unrealized PnL and position via the info endpoint.

**Comparison:** If `abs(local_pnl - exchange_pnl) > divergence_threshold`:
- Log a warning with both values.
- **Adopt the exchange's numbers for all subsequent risk decisions** (drawdown checks, cap enforcement).
- Continue using local ledger for analytics, but flag it as potentially inaccurate.

This catches subtle drift from missed fills, rounding differences, or funding payments not tracked locally.

---

## 11. Default Parameters

### 11.1 BTC-PERP Starter Configuration

```yaml
# === Core Grid ===
leverage: 2.0
levels_per_side: 25
grid_step_bps: 15-25      # dynamically adjusted (see spacing formula)
capital_allocation: 0.70   # 70% of total risk budget

# === Expansion Grid ===
expansion_levels_per_side: 15
expansion_step_mult: 1.5   # expansion step = core step * 1.5
expansion_range_atr: 4.0
expansion_allocation: 0.30 # 30% of BTC's risk budget (not total)

# === Anchoring ===
anchor_shift_threshold_atr: 1.5
anchor_delay_minutes: 30

# === Inventory ===
soft_cap_pct: 0.50         # 50% of max_abs_position
max_abs_position: <derived at pre-flight from this asset's share of the
                    shared risk budget — total_risk_budget_pct * asset_weight
                    * capital_allocation * leverage, converted to units via
                    mid_price; see section 9.1. Not a config literal.>

# === Breakout ===
breakout_atr_distance: 4.5    # absolute ATR distance from anchor (must exceed expansion range)
cooldown_minutes: 30

# === Volatility ===
vol_pause_threshold: "top 20th percentile of trailing 7d"
vol_kill_threshold: "top 5th percentile of trailing 7d"
vol_recovery_minutes: 10

# === Funding ===
funding_moderate_threshold_annualized: 0.30   # 30%
funding_extreme_threshold_annualized: 1.00    # 100%

# === Drawdown ===
max_daily_drawdown_pct: 0.03    # 3%
max_weekly_drawdown_pct: 0.07   # 7%

# === Operational ===
reconcile_interval_seconds: 5
max_consecutive_errors: 10
max_time_desynced_seconds: 30
ws_reconnect_stale_seconds: 10  # WS health-monitor reconnect trigger; must stay
                                 # below max_time_desynced_seconds so reconnection
                                 # has a window to clear desync before the kill switch fires
stagger_initial_levels: 5       # nearest 5 per side placed immediately

# === Order Execution ===
maker_only: true                # Post-Only for all grid orders
taker_allowed: "emergency flatten only (IOC)"
post_only_max_retries: 3

# === Dynamic Slippage (section 5.7) ===
base_slippage_bps: 1.5           # normal-conditions floor for grid slippage buffer
vol_slippage_scale: 2.0          # sensitivity of slippage buffer to vol increase
min_slippage_bps: 1.0            # hard floor on grid slippage buffer
max_slippage_bps: 10.0           # hard cap on grid slippage buffer
depth_impact_scale: 1.0          # calibration constant for flatten slippage estimate (tune on testnet)

# === Emergency Flatten (section 6.7) ===
max_flatten_slippage_bps: 50     # IOC limit price bound for emergency flatten
flatten_time_budget_seconds: 10  # max time to attempt flatten before entering dead state
flatten_tranche_pct: 0.80        # fraction of visible depth to consume per tranche
flatten_retry_pause_ms: 300      # pause between tranches for book refill

# === Server-Side Backstop (section 6.8) ===
backstop_buffer_atr: 1.0         # gap between bot's breakout threshold and exchange-side stop
```

### 11.2 ETH-PERP Starter Configuration

Same structure as BTC with these adjustments:

```yaml
capital_allocation: 0.40         # 40% of total risk budget
grid_step_bps: 18-30             # ETH typically wider spreads than BTC
expansion_range_atr: 3.5         # tighter than BTC (4.0) to maintain 0.5 ATR buffer to breakout threshold
breakout_atr_distance: 4.0       # ETH breaks out more sharply; trigger earlier (tighter than BTC's 4.5)
max_flatten_slippage_bps: 75     # wider than BTC due to lower liquidity
```

### 11.3 Portfolio-Level Parameters

```yaml
total_risk_budget_pct: 0.10      # 10% of account equity
btc_weight: 0.60
eth_weight: 0.40
portfolio_delta_mult: 0.75       # portfolio cap = 75% of sum of individual caps
pnl_crosscheck_interval_seconds: 60
pnl_divergence_threshold_usd: 50 # alert if local vs exchange PnL diverge by this much
```

---

## 12. Decision Log

A record of every significant design decision, including what was changed during the design review and why.

### 12.1 Original Design Decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Grid mode | Anchored adaptive | Static grids become stale as price drifts. Adaptive re-centering keeps the grid productive. |
| Funding handling | Hard filter (pause) | *Revised in review — see 12.2.* |
| Breakout response | Cancel + flatten | Safest option. Bounded loss. Most retail grids fail because they don't do this. |
| Order sizing | Volatility-scaled | Keeps risk-per-fill consistent across market regimes. |
| Target assets | BTC + ETH | Most liquid HL perps. Best grid candidates. |
| Maker vs taker | Maker-only for grid, taker only for emergency | Preserves maker fee advantage. Taker fees acceptable only when capital is at risk. |

### 12.2 Design Review Changes

| # | Finding | Change Made | Reasoning |
|---|---|---|---|
| 1 | **Reconciliation race conditions** | Added WS `orderUpdates` as primary state driver; REST reconciliation demoted to backup/consistency check. | REST-only polling creates 2–10s blind spots. Fills during that window cause inventory tracking drift. Dual-path gives real-time responsiveness with guaranteed eventual consistency. |
| 2 | **Batch order operations** | All order operations (cancel + place) submitted as single batch requests. Never individual calls. | Running 2 assets × 40 levels = 80+ operations per cycle. Individual calls saturate rate limits and create partial-grid timing windows. Batch is atomic. |
| 3 | **Multi-layer complexity** | Reduced from 3 grid layers to 2 (Core + Expansion). Recovery layer deferred. | Third layer adds parameters, activation conditions, and inventory interactions with no backtest data to justify. Two layers cover core chop + mild drift. Recovery can be added when validated. |
| 4 | **Anchor re-centering fragile** | Added regime confirmation + vol stability as mandatory conditions alongside time delay. | Timer-only re-anchoring can chase trends. Price may have drifted for 30 minutes, but if vol is rising or regime reads TREND, re-anchoring is wrong. Multi-condition gate prevents this. |
| 5 | **Funding filter too blunt** | Changed from hard-pause-only to two-tier (moderate→skew, extreme→pause). | Hard pause at moderate funding removes the bot from good mean-reversion setups. Extreme funding often precedes reversion. Two-tier captures nuance while still protecting against worst case. |
| 6 | **Correlation guard under-specified** | Replaced vague "tighten caps if correlated" with portfolio-level delta cap (75% of sum of individual caps). | BTC/ETH correlation is ~0.85 constantly — it's a structural constant, not a variable to measure. Portfolio delta cap handles it implicitly without needing correlation windows or thresholds. |
| 7 | **Client order ID edge case** | Added `grid_config_hash` (anchor + range + step) to the order ID scheme. | During re-anchor, old and new grids can have overlapping price levels. Without config identifier, bot might incorrectly adopt old orders as belonging to new grid. Config hash eliminates ambiguity. |
| 8 | **No exchange maintenance handling** | Added maintenance-awareness mode: passive wait, errors don't count toward kill switch, full reconciliation on reconnect. | Without this, a routine 5-minute maintenance window trips `max_consecutive_errors` and requires manual restart. Maintenance errors are not bot errors. |
| 9 | **PnL relies on local fills only** | Added periodic cross-check against exchange-reported PnL. Exchange numbers override local for risk decisions. | Missed WS messages, partial fill edge cases, or funding payments not tracked locally can cause drift. Risk decisions must trust exchange state. Local ledger is for analytics. |
| 10 | **No graceful shutdown** | Added SIGTERM/SIGINT handler: stop loop → cancel orders → persist state → exit clean. Do NOT flatten on graceful shutdown. | Without this, every restart leaves orphaned orders and requires full reconciliation. Cancel-but-don't-flatten avoids unnecessary taker fees on planned restarts. |
| 11 | **Grid step ignores spread** | Added live bid-ask spread as a component in the grid step floor calculation. | Static fee formula misses the spread, which can widen significantly on HL during vol spikes. If spread > grid step, every fill is guaranteed loss. Live spread inclusion auto-widens or pauses the grid. |

### 12.3 Second Design Review — Clarifications & Enhancements

| # | Type | Finding | Change Made | Reasoning |
|---|---|---|---|---|
| 12 | **Clarification** | **Breakout detection range ambiguous** — "upper_range" in breakout formula could mean Core range (±2.5 ATR) or Expansion range (±4 ATR). If Core: Expansion levels beyond 3.75 ATR are unreachable (dead order slots). If Expansion: breakout response is delayed, weakening the primary safety mechanism. | Replaced relative `breakout_k_atr` with absolute `breakout_atr_distance` (default 4.5 ATR from anchor), independent of active layers. Expansion range (4 ATR) fits within this with a 0.5 ATR buffer. | A fixed absolute threshold eliminates ambiguity, doesn't shift when layers activate/deactivate, and makes the safety boundary deterministic. The buffer allows the outermost Expansion levels to fill before breakout fires. |
| 13 | **Enhancement** | **Flip orders orphaned during re-anchoring** — reconciliation cancels orders not in the desired set. After re-anchoring, flip orders from old-grid fills may not match any new-grid level. The profit-taking order is cancelled; the position becomes inventory-skew-managed at potentially worse prices. | Added section 7.6: "pending flips" set that persists across re-anchor events. Flip orders are included in the desired set regardless of current anchor. Removed only when filled or position unwound by other means. Persisted to StateStore. | Re-anchoring is infrequent (multi-condition gate), but when it happens, silently converting expected profitable flips into unmanaged inventory is an unforced error. The pending flips set is a small addition (list of tuples) with clear lifecycle rules. |
| 14 | **Clarification** | **Drawdown calculation basis unspecified** — doc didn't define whether drawdown includes unrealized PnL, or whether "daily" means calendar day vs rolling window. Calendar days create a boundary exploit; realized-only ignores accumulating adverse positions. | Specified: realized + unrealized combined, rolling windows (24h / 168h), exchange-reported PnL as authoritative source. Documented the wick-flatten-reverse tradeoff and why it's accepted. | Unrealized must be included — ignoring it defeats the purpose of the limit. Rolling windows prevent the calendar-boundary exploit. The wick tradeoff is bounded and small; the alternative is unbounded. |
| 15 | **Clarification** | **Staggered placement + fill interaction unclear** — if the outermost placed level fills and the flip targets a queued level, the doc didn't specify whether the queued level is promoted immediately or waits for the next stagger cycle. | Added "fill-triggered level promotion" to section 5.3: a fill on any placed level immediately promotes the next queued level on that side. Deterministic order IDs prevent duplication if the promoted level and flip order target the same price. | Without explicit promotion, there's a gap in coverage after a fill at the stagger boundary. The dedup via order IDs makes this safe even if both paths (flip + promotion) target the same level. |
| 16 | **Note** | **Low-vol regime interaction between step and size** — in low vol, ATR-based step shrinks (toward fee floor) while vol-scaled size increases (toward max_size). Tighter spacing + larger orders = rapid inventory accumulation if vol then spikes. | Added explanatory note to section 5.5 documenting the interaction. No structural change — `max_size` clamp and fee floor are the existing defenses. Noted that `max_size` should be set based on risk tolerance, not exchange maximums. | This is a parameter tuning concern, not a design flaw. The existing clamps handle it, but the interaction is non-obvious and worth documenting so the implementer sets `max_size` conservatively. |
| 17 | **Enhancement** | **Emergency flatten has no retry protocol** — a single IOC that partially fills or times out leaves residual exposure during a crisis. The design said "flatten using IOC/market orders" with no handling for partial fills, API timeouts, or thin books. | Added section 6.7: flatten state machine with pre-flatten depth assessment, chunked IOC tranches with bounded slippage limits, partial fill retry loop with time budget (10s), slippage escalation on failure, and dead-state fallback with critical alert. Updated sections 6.3, 6.4, 6.6 to reference the protocol. | During extreme moves, the order book thins and a single IOC may not fill completely. The retry loop with escalating slippage ensures the bot keeps trying within a time budget, then fails safely (dead state + alert) if it can't flatten — never silently leaving residual exposure. |
| 18 | **Enhancement** | **No protection if bot process dies during a crisis** — all risk mechanisms depend on the bot being alive and connected. A crash, VPS failure, or network outage during a breakout leaves the position completely unmanaged. | Added section 6.8: server-side stop-loss backstop using HL trigger orders (`reduce_only=true`, trigger market, `tpsl="sl"`). Maintained automatically as position changes. Set 1.0 ATR wider than bot's own breakout threshold so bot acts first under normal conditions. Updated section 2.4 to budget order slots. | HL trigger orders execute server-side on mark price regardless of client connectivity. This is the last line of defense — it costs 1 order slot per asset (negligible) and provides protection against the one scenario no client-side logic can handle: the client not running. |
| 19 | **Enhancement** | **Slippage buffer is static (1–2 bps) and flatten slippage is unmodeled** — the worst-case loss formula treats flatten slippage as a small additive constant, but during breakouts (when flattens occur) slippage scales with position size, spread, and book depth. The formula understates actual risk. | Added section 5.7: dynamic slippage model. Grid slippage buffer scales with realized vol. Flatten slippage modeled as `f(position, spread, depth)`. New flattenability constraint dynamically reduces position hard cap when liquidity thins. Pre-flight check validates worst-case loss vs drawdown limit. | The static buffer is fine for grid orders (Post-Only, zero execution slippage). But flatten slippage dominates worst-case loss and scales with the exact conditions that trigger flattening. The flattenability check closes the loop — position limits adapt to current liquidity, ensuring the bot never accumulates a position it can't exit within budget. |

### 12.4 Third Design Review — Parameter Consistency Fixes

| # | Type | Finding | Change Made | Reasoning |
|---|---|---|---|---|
| 20 | **Bug** | **ETH expansion range equals breakout threshold (zero buffer)** — ETH inherits `expansion_range_atr: 4.0` from BTC defaults but overrides `breakout_atr_distance` to 4.0. Section 6.3 requires a buffer between the Expansion outer edge and the breakout threshold ("0.5 ATR buffer… provides a narrow window for mean-reversion at extreme levels before the safety mechanism fires"). With zero buffer, the outermost Expansion levels sit exactly at the breakout distance — they either never fill (breakout fires simultaneously) or create a race between Expansion fill processing and breakout detection. | Added `expansion_range_atr: 3.5` to ETH config (section 11.2). This restores a 0.5 ATR buffer between ETH's Expansion outer edge (3.5 ATR) and its breakout threshold (4.0 ATR), matching the buffer ratio in the BTC config (4.0 vs 4.5). | The tighter ETH breakout distance is correct (ETH breaks out more sharply). The fix is on the Expansion side: fewer Expansion levels over a narrower range, which is consistent with ETH's tighter breakout behavior — less room to speculate on mean-reversion at extremes when the safety boundary is closer. |
| 21 | **Bug** | **Flattenability constraint formula omits spread component** — the flatten slippage formula is `spread + (pos / depth) * scale`, but the derived `max_flattenable_position` was `max_slippage / scale * depth` — solving as if spread is zero. This overstates the flattenable position. Example: at `max_slippage=50 bps`, `spread=10 bps`, `scale=1.0`, `depth=100`, the old formula yields max position 5000 (actual slippage at exit: 60 bps, exceeding budget) vs correct value of 4000 (actual slippage: 50 bps). | Corrected formula in section 5.7 to `(max_flatten_slippage_bps - current_spread_bps) / depth_impact_scale * recent_avg_depth`. Added explanatory note on why the subtraction is required for consistency with the slippage estimation formula. | The flattenability constraint must be the exact inverse of the slippage estimation — both formulas operate on the same model. The spread is a fixed cost of any flatten (paid regardless of position size), so only the remainder of the slippage budget is available for position-dependent impact. Without the correction, the constraint is permissive by exactly `spread / scale * depth`, which grows when spreads widen — the worst time to be permissive. |

---

*End of design document.*
