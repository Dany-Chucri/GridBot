Below is a full end-to-end design for a Hyperliquid perps grid bot in Python, incorporating everything we discussed: regime filtering, inventory caps, breakout handling, volatility circuit breakers, funding awareness, fee/slippage realism, reconciliation, persistence, and operational reliability — no loose ends.

0) Ground rules and constraints (Hyperliquid-specific)

Connectivity

Use WebSocket for real-time prices: wss://api.hyperliquid.xyz/ws (mainnet) / wss://api.hyperliquid-testnet.xyz/ws (testnet).

Use the official Python SDK repo (or compatible client).

Order types & flags we will rely on

GTC (resting), Post Only (ALO) (maker-only), IOC (liquidity-taking), plus Reduce Only (position reducing).

Operational limits

Open order limit defaults are large, but still finite; design should avoid “one order per grid level forever” if you plan many assets/levels.

1) What this bot is (and isn’t)

Goal: harvest mean-reversion / chop by placing symmetric limit orders around price, while preventing the two classic grid deaths:

Trend breakouts → runaway inventory + liquidation

Micro grid → fees & slippage eat edge

Not a goal: beating professional market makers on pure latency. This is a robust retail/pro-sumer design meant to survive regimes.

2) Core architecture
Modules

MarketData

WS subscribe to mid/mark + trades

maintains latest price, rolling returns, realized vol, ATR proxy

StateStore

persistent storage (SQLite or Postgres)

stores: current regime, active grid spec, open orders snapshot, last reconcile time, net position, fills ledger

GridEngine

calculates grid levels from anchor + range + spacing

“desired orders” set (prices, side, size, flags)

OrderManager

idempotent placement/cancel/replace

supports Post-Only for maker edge (with fallback logic)

handles common HL errors (bad post-only price, reduce-only rejection, etc.)

RiskManager

leverage & liquidation buffer checks

max inventory cap enforcement

circuit breakers: volatility spike, drawdown, connectivity loss

breakout detection + flatten logic

PnL/Funding Monitor

tracks realized/unrealized PnL from fills

monitors funding bias (if funding extreme, adapt / pause)

Supervisor

main event loop

periodic reconciliation (truth = exchange)

alerting + logging

3) Strategy definition: the grid itself
3.1 Grid type (tradeoff)

Choice A: Static range grid

Simpler and predictable

Dies more often if range becomes stale

Choice B: Anchored adaptive range

Range can re-center periodically in calm conditions

Much more survivable in real markets

Default I recommend: Anchored adaptive, with strict rules about when re-anchoring is allowed (to avoid chasing breakouts).

3.2 Price inputs

Use mid price (from best bid/ask) for grid placement logic, and mark price for risk checks if available (perps risk). If the SDK exposes both, store both; if not, use mid for signals and keep safety buffers.

3.3 Grid spacing (fee/slippage-aware)

A grid only has an edge if step size clears frictions.

Define:

maker_fee (conservative estimate)

slippage_buffer (bps)

min_edge_bps = 2*maker_fee + slippage_buffer + safety_margin

Then choose:

grid_step_bps >= min_edge_bps

Tradeoff question: do you want maker-only behavior (Post Only) or allow occasional taker fills for inventory/risk management?

Default: maker-only for grid entries/exits, allow taker only for emergency flatten.

Post Only (ALO) exists explicitly for “don’t cross the spread” behavior.

3.4 Levels, range, and sizing

Let:

levels_per_side = N (e.g., 20–50)

step = grid_step_bps * mid_price

range = N * step

Each level has size q (in contracts/base units).

Sizing tradeoff: constant size vs volatility-scaled size

Constant size is simplest.

Volatility-scaled size keeps risk consistent across regimes.

Default: volatility-scaled or constant with a hard inventory cap (see below).

4) Risk model: the make-or-break part
4.1 Leverage and liquidation buffer

Even a “safe” grid can accumulate inventory in a trend.

Rules:

Use low leverage (1–3x) for the grid engine.

Require liquidation price to be outside a “worst-case wick zone”:

liq_distance >= (grid_range * liq_buffer_mult), e.g. 2–3× grid range.

If you can’t guarantee that buffer with your leverage + sizing, don’t run.

4.2 Inventory cap + inventory-aware quoting

Define:

max_abs_position (hard cap)

soft_cap (start biasing orders before hard cap)

Behavior:

If abs(pos) >= hard cap: cancel any orders that would increase exposure; only place reduce-only orders to unwind.

If abs(pos) >= soft cap: skew the grid:

reduce order sizes in the direction that increases exposure

increase order sizes on the unwind side (still within max order size limits)

Why reduce-only matters: Hyperliquid enforces reduce-only semantics and will reject if it would increase position.

4.3 Breakout detector (trend protection)

When price exits the intended range, you must decide: pause, recenter, or flatten.

Define breakout conditions (example robust set):

breakout = (mid_price > upper_range + k*ATR) OR (mid_price < lower_range - k*ATR)

or |return_5m| > threshold / realized vol spike

Actions (recommended):

Immediate: cancel all resting grid orders

If inventory is significant: flatten to zero (or to a small “core” position) using market/IOC

Cooldown: wait cooldown_minutes

Re-evaluate regime: only restart if regime filter says “range”

This is the single biggest thing most retail grids omit.

4.4 Volatility circuit breaker

Define a rolling vol metric (realized stdev of 1s/5s returns; or ATR proxy from minute candles).

Rules:

If vol exceeds vol_pause_threshold, stop placing new orders

If vol exceeds vol_kill_threshold, cancel + flatten

Restart only after vol normalizes for T minutes

4.5 Funding awareness (perps-specific)

Funding extremes can turn “grid profits” into a slow bleed if you carry inventory.

Rules:

Monitor funding rate (if accessible via info endpoint / SDK).

If funding is extreme:

either pause grid,

or bias exposure toward the paid side (careful: that becomes directional).

Tradeoff question: do you want funding to be a hard filter (pause) or a soft bias (skew sizes)?

Default: hard filter at first (simpler + safer).

(Info endpoint exists for exchange/user data retrieval. )

4.6 Drawdown and “bot sanity” limits

Add absolute safety stops:

max_daily_drawdown

max_weekly_drawdown

max_consecutive_errors

max_time_desynced_seconds (if position/order state can’t reconcile)

If tripped: cancel orders + flatten + disable until manual restart.

5) Regime filter (only run grid when chop is likely)

Grid runs only when the market is “range-like.”

A robust filter uses multiple signals:

Volatility not too high (below threshold)

Trend strength low:

price within band around a moving average (e.g., within X·ATR of EMA)

or ADX-like proxy (optional)

No breakout in last cooldown window

Output:

REGIME = RANGE | TREND | HIGH_VOL | UNKNOWN

Only in RANGE do we run the full grid.

6) Order placement & lifecycle
6.1 Desired order set

At any point, GridEngine produces a set of intended orders:

buys at levels below anchor

sells above anchor

include flags:

Post Only (ALO) for grid orders

GTC time-in-force for resting orders

Reduce Only only for unwind orders

6.2 Reconciliation (truth comes from exchange)

Every reconcile_interval (e.g., 2–10 seconds):

Fetch open orders

Fetch position

Compare with intended orders

Apply minimal diff:

cancel orders not in desired set

place missing ones

replace stale ones if price anchor/range shifted

Critical: orders must be idempotent.

Use deterministic client order IDs per (symbol, level_price, side, epoch) so restarts don’t duplicate.

6.3 Post-only rejection handling

Hyperliquid can reject post-only if it would immediately match (common “BadAloPx”-style condition).

Handling:

If Post Only rejected, nudge price one tick farther from mid and retry (bounded retries)

If still rejected, skip that level this cycle

6.4 Partial fills

When a level partially fills:

update remaining qty in state

once fully filled, immediately create the opposite order one step away (classic grid “flip”)

But if you’re near inventory caps, you may choose to unwind instead of continuing to add.

7) State, persistence, and restart safety (no loose ends)
7.1 What must be stored

bot config version + parameters

current anchor price + range + step + levels

current regime + timestamps

last known position + average entry (if provided)

map: level_price -> order_id -> status -> remaining_qty

fills ledger (for PnL)

last heartbeat times

7.2 Restart behavior

On startup:

Load config

Query exchange for open orders + position

Cancel unknown orders (or adopt them if matching a known grid epoch)

Rebuild desired grid based on current regime rules

Resume reconciliation loop

This prevents “ghost orders” or doubled grids after a crash.

8) Deployment & ops
Minimum operational requirements

Run on a VPS close-ish to endpoints (latency helps but isn’t everything)

Use systemd / supervisor to auto-restart

Use structured logs (JSON or consistent format)

Alerts (Telegram/Discord/email) on:

kill switch triggered

reconcile failures

drawdown exceeded

repeated post-only rejections

position exceeds cap

Testnet first

Hyperliquid has a testnet WS endpoint.
Do:

48–72h testnet soak

then tiny size on mainnet

then scale gradually

9) Default parameter set (safe starter)

For BTC-PERP, starter defaults (adjust once you answer the tradeoffs below):

leverage: 1.5x–2x

levels_per_side: 25

grid_step_bps: 12–25 bps (depends on fees/spread)

soft_cap: 50% of max_abs_position

max_abs_position: sized so liquidation buffer remains > 2× range

breakout_k_atr: 1.0–1.5

cooldown_minutes: 30

vol_pause_threshold: percentile-based (e.g., top 20% of last 7d)

vol_kill_threshold: top 5% (or absolute spike rule)

maker_only: true for grid orders

taker_allowed: only for emergency flatten (IOC/market)

(Order types/flags referenced are supported by HL. )



TRADEOFFS DECIDED:
Grid mode: Anchored adaptive

Funding handling: Hard filter (pause when extreme)

Exit on breakout: Cancel + flatten (safest) 

Sizing: Volatility-scaled per level (recommended once stable)

Target asset(s): BTC+ETH




Staggering Considerations:

1️⃣ The Core Problem: Adaptive Grids Can Accidentally Trend-Chase

If a grid re-anchors too aggressively:

Price breaks up

Grid recenters higher

Price mean-reverts

Bot now buys high

This is classic trend-chasing.

The solution is staggered anchoring + delayed participation.

2️⃣ Solution: Multi-Anchor Staggered Grids

Instead of a single grid, the bot manages multiple anchor layers.

Example:

Anchor Layer 1 (core grid)
Anchor Layer 2 (expansion grid)
Anchor Layer 3 (breakout recovery grid)

Each activates under different conditions.

Layer 1 — Core Grid

Your normal anchored adaptive grid.

Characteristics:

Tight spacing

Highest capital allocation

Only active in confirmed range regime

Example:

range = ±2.5 ATR
levels_per_side = 25
grid_step ≈ 0.15–0.25%
capital_allocation = 60%

This grid is disabled immediately on breakout.

Layer 2 — Expansion Grid

This grid activates outside the core range but before breakout flattening.

Purpose:

Prevent the bot from re-anchoring too quickly

Harvest mean reversion on mild breakouts

Example trigger:

|price - anchor| > core_range
AND
|price - anchor| < breakout_threshold

Characteristics:

range = ±4 ATR
levels_per_side = 15
grid_step = larger
capital_allocation = 25%

Spacing is wider so the bot doesn't overtrade during expansion.

Layer 3 — Recovery Grid (Optional but powerful)

After a breakout flatten event, instead of immediately restarting the core grid, you run a very wide probing grid.

Purpose:

Slowly re-enter the market

Avoid restarting the bot right before another trend continuation

Example:

range = ±6–8 ATR
levels_per_side = 8–12
very small size
capital_allocation = 15%

This grid acts like a market probe.

Once volatility normalizes and the regime returns to RANGE, the bot reinstates the Core Grid.

3️⃣ Delayed Anchor Re-centering

This is crucial.

Never re-anchor immediately when price moves.

Instead:

if abs(price - anchor) > anchor_shift_threshold
    start timer

if timer > anchor_delay
    re-anchor

Example values:

anchor_shift_threshold = 1.5 ATR
anchor_delay = 20–40 minutes

This prevents reacting to short-lived spikes.

4️⃣ Volatility-Scaled Order Sizing

You chose volatility scaling. Here’s the robust way to implement it.

Let:

vol = realized volatility (or ATR)
target_risk = % of account per level

Then:

order_size = target_risk / vol

Higher volatility → smaller orders.

Example clamp:

min_size <= order_size <= max_size

This keeps exposure stable across regimes.

5️⃣ Trend-Chasing Protection: Momentum Filter

Even if the regime filter says RANGE, we add a micro-trend blocker.

Disable new entries if:

price_change_5m > 1.2 ATR
OR
price_change_1m > 0.5 ATR

Existing grid orders can remain, but no new levels are added.

This prevents stacking orders during sudden directional moves.

6️⃣ Staggered Order Placement (Microstructure Layer)

Instead of placing all levels instantly:

place_levels = nearest 5 per side
queue remaining levels

Then gradually add levels as:

fills occur

price moves

Benefits:

avoids huge order bursts

adapts liquidity usage

reduces rate-limit pressure

(Hyperliquid has rate limits and open order limits to consider. Their docs specify per-user limits and open order constraints.) Hyperliquid

7️⃣ BTC + ETH Portfolio Handling

BTC and ETH grids must share risk budget.

Define:

total_risk_budget = 10% account exposure
btc_weight = 0.6
eth_weight = 0.4

Inventory caps become:

btc_cap = total_risk_budget * btc_weight
eth_cap = total_risk_budget * eth_weight

This prevents both grids from maxing out simultaneously.

Also apply a correlation guard:

If both assets trend in the same direction strongly, tighten exposure caps.

8️⃣ Final Execution Flow

Main loop every ~1–3 seconds:

update_market_data()

update_volatility()

detect_regime()

if breakout:
    cancel_orders()
    flatten_position()
    start_cooldown()

if cooldown_active:
    run_recovery_grid()
else:
    run_core_grid()
    run_expansion_grid()

apply_inventory_caps()

reconcile_orders()

log_state()
9️⃣ Expected Behavior

In sideways markets:

Core grid dominates

Many small mean-reversion trades

During mild expansion:

Expansion grid activates

Core grid pauses

During strong trends:

Breakout trigger fires

Positions flattened

Recovery grid probes slowly

🔟 Grid Spacing Basis

What professional market makers often do

Hybrid:

grid_step = max(
    ATR_based_step,
    fee_minimum_step
)