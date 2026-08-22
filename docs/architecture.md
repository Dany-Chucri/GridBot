# Architecture Overview

> Implementation guide for the module architecture defined in [design.md](design.md) section 3.

## Module Dependency Graph

```
Supervisor (orchestrator)
├── MarketData        ← WS/REST, no order ops
├── StateStore        ← SQLite, no exchange contact
├── GridEngine        ← pure calculation, no I/O
├── OrderManager      ← ONLY module that sends orders
├── RiskManager       ← evaluates constraints, returns decisions
└── PnLMonitor        ← analytics, cross-checks with exchange
```

**Hard rules:**
- `GridEngine` has zero side effects. It receives state, returns desired orders.
- `OrderManager` is the sole gateway to exchange order operations.
- `RiskManager` never places or cancels orders. It returns a `RiskDecision` that the `Supervisor` acts on.
- `MarketData` owns the WS connection and provides prices/fills to other modules.

## Data Flow Per Cycle

See [design.md](design.md) section 3.3 for the authoritative cycle specification.

```
MarketData.update()          →  fresh prices, vol metrics, fills
RiskManager.evaluate()       →  RiskDecision (continue / skew / flatten / kill)
  ↓ if CONTINUE
GridEngine.compute_desired() →  list[DesiredOrder]
OrderManager.reconcile()     →  batch cancel+place diff
StateStore.save()            →  persist updated state
PnLMonitor.crosscheck()      →  validate local vs exchange PnL
```

## Anchor Lifecycle

`GridEngine` is pure and never mutates `state.grid_config` itself ([design.md](design.md) section 5.1). `Supervisor._maintain_anchor` owns the anchor's lifecycle, once per cycle, whenever `regime == RANGE`:

- **No `grid_config` yet** (fresh asset, or a restart that lost it): establish one immediately via `GridEngine.new_grid_config(mid_price, ...)`. This runs even under `SUPPRESS_NEW_ENTRIES`, so the anchor is ready the instant new entries are allowed again instead of costing an extra cycle.
- **`grid_config` exists**: track `state.drift_start_ms` against `state.grid_config.anchor`, then defer to `GridEngine.should_reanchor` (the four-condition gate) and `GridEngine.compute_new_anchor`. On a re-anchor, `state.anchor_epoch` increments and `state.stagger_placed_count` resets so the new grid deploys staggered rather than all at once.

`RiskManager.is_vol_stable_or_declining` supplies condition 4 (vol not rising), comparing the trailing 15-minute window's two halves.

## Concurrency Model

- Single async event loop (`asyncio`).
- WS messages and REST reconciliation update the same `AssetState`, serialized via `asyncio.Lock` to prevent races ([design.md](design.md) section 4.3).
- Each asset runs its cycle independently within the same loop.

## State Ownership

| Data | Owner | Consumers |
|---|---|---|
| Prices, vol metrics | MarketData | GridEngine, RiskManager |
| Regime | RiskManager | GridEngine, Supervisor |
| Desired orders | GridEngine | OrderManager |
| Open orders, position | MarketData (WS+REST) | OrderManager, RiskManager |
| Persisted state | StateStore | Supervisor (recovery) |
| PnL ledger | PnLMonitor | RiskManager (drawdown) |

## Key Files

| File | Module | Design Doc Section |
|---|---|---|
| `gridbot/market_data.py` | MarketData | 4.1-4.3 |
| `gridbot/state_store.py` | StateStore | 4.4 |
| `gridbot/grid_engine.py` | GridEngine | 5.1-5.7, 7.1, 7.3, 7.6 |
| `gridbot/order_manager.py` | OrderManager | 7.2-7.5, 6.7-6.8 |
| `gridbot/risk_manager.py` | RiskManager | 6.1-6.6, 8.1-8.3, 9.2 |
| `gridbot/pnl_monitor.py` | PnLMonitor | 10.6 |
| `gridbot/supervisor.py` | Supervisor | 3.3, 4.4-4.5, 10.2-10.4 |
| `gridbot/types.py` | Shared types |, |
| `gridbot/config.py` | Configuration | 11.1-11.3 |
