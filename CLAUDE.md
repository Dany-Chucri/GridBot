# GridBot

Hyperliquid perpetual futures grid trading bot (BTC-PERP, ETH-PERP). Python. Design doc, **MUST READ**: `docs/design.md` IN ITS ENTIRETY. When referred to a specific planning document, be sure to read said plan in its entirety.

## Core Invariants — Do Not Violate

- **Exchange is truth.** All risk decisions use exchange-reported state. Local state is a cache for speed, never for authority.
- **Batch all order ops.** Never send individual order calls. Cancel + place as a single batch request. Partial-grid states are unacceptable.
- **Post-Only (ALO) for all grid orders.** Taker fills only via IOC during emergency flatten. No exceptions.
- **Reduce-Only when at hard cap.** Orders that would increase exposure past hard cap must carry the reduce-only flag.
- **Flatten is a state machine** (section 6.7), not a single IOC. Chunked tranches, retry loop, slippage escalation, dead-state fallback. Never assume a single IOC will fully fill.
- **Backstop stop-losses** (section 6.8) must exist server-side for every open position. They are the dead-man's switch.

## What Not To Do

- **Don't perform pass-by refactors.** All changes should be clear, deliberate, and relevant to the prompt.
- **Don't invent safety mechanisms not in the design.** The risk model is carefully layered. Adding ad-hoc guards creates interaction bugs. If you think something is missing, flag it.
- **Don't use REST polling as the primary state path.** WS `orderUpdates` is primary; REST is the backup consistency check. Swapping this creates 2-10s blind spots.
- **Don't flatten on graceful shutdown.** Cancel orders, persist state, exit. Flattening wastes taker fees on planned restarts.
- **Don't re-anchor eagerly.** Re-anchoring requires ALL four conditions (drift > threshold, delay elapsed, regime == RANGE, vol stable/declining). Relaxing any one causes trend-chasing.
- **Don't flip on partial fills.** Only flip when a level is fully filled. Partial-flip creates unhedged exposure.
- **Don't couple BTC and ETH regimes.** Each asset has independent regime detection. Portfolio-level risk is handled by the delta cap, not regime coupling.
- **Don't hardcode slippage as a small constant.** Grid slippage buffer scales with vol. Flatten slippage is `f(position, spread, depth)`. The flattenability constraint dynamically tightens hard caps.
- **Don't cancel pending flip orders on re-anchor.** They are in the desired set regardless of current grid config (section 7.6). Cancelling them orphans expected profit.
- **Don't count maintenance errors toward the kill switch.** 503/connection-refused during exchange downtime is not a bot error.

## Tracking

### REPO_MAP.md

Maintain `REPO_MAP.md` at the project root. Update it whenever files are added, removed, renamed, or their purpose changes. Format:

```
# Repo Map

## directory/
- `file.py` — one-line description of purpose
```

Group by directory. One line per file. Keep alphabetical within groups.

### progress.md

Write to `progress.md` at the end of every task. Use this exact format:

```
## YYYY-MM-DD — Short title

**Goal:** What was the objective.

**Changes:**
- Bullet per logical change (not per file)

**Files modified:**
- `path/to/file.py` — what changed in it

**Files added:**
- `path/to/new_file.py` — purpose

**Files removed:**
- `path/to/old_file.py` — why

**Status:** Complete | Partial (what remains)

**Notes:** Anything non-obvious (tradeoffs, deferred work, open questions). Omit if none.

---
```

Append new entries at the top (most recent first). Never delete old entries.

## Documentation (`docs/`)

- `docs/design.md` is the authoritative design reference. Do not contradict it. If implementation diverges, flag the conflict and make a glaring note.
- When a new feature or module is implemented, ensure it is documented in `docs/`. One doc per major subsystem or concept. Keep docs concise: what it does, how it works, key decisions. No filler.
- Update existing docs when behavior changes. Stale docs are worse than no docs.
- Don't duplicate content across docs. Cross-reference with relative links instead.

## Architecture

Six modules + supervisor. GridEngine is pure calculation (no side effects). OrderManager is the only module that talks to the exchange for order ops. RiskManager enforces all safety constraints. See `docs/design.md` section 3 for data flow.

## Parameters

Default configs in `docs/design.md` sections 11.1-11.3. ETH differs from BTC: tighter breakout (4.0 vs 4.5 ATR), narrower expansion (3.5 vs 4.0 ATR), wider flatten slippage (75 vs 50 bps).
