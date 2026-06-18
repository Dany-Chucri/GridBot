# GridBot

Autonomous grid trading bot for Hyperliquid perpetual futures (BTC-PERP, ETH-PERP). Harvests mean-reversion profit in sideways markets using symmetric limit orders around a dynamic price anchor.

## How It Works

The bot places a symmetric grid of Post-Only (maker) limit orders above and below an anchored mid-price. When price oscillates, buy orders fill on dips and sell orders fill on rallies — each round trip earns the grid spread minus fees. When price breaks out of range, a layered risk system suspends trading, flattens the position if necessary, and waits for regime to recover.

Key behaviors:
- **Regime-gated**: trading only runs in `RANGE` regime; pauses automatically on breakout/high-vol detection
- **Batch-atomic order ops**: all cancel + place operations sent as a single batch request — no partial-grid states
- **Exchange-authoritative**: all risk decisions use exchange-reported state, never local cache
- **Crash-recoverable**: SQLite state store persists fills, grid spec, and position through restarts
- **Server-side backstop**: one stop-loss order per asset always rests on the exchange as a dead-man's switch

## Architecture

Six modules orchestrated by a Supervisor event loop:

| Module | Role |
|---|---|
| `market_data` | WebSocket price feed, fill events, volatility metrics |
| `grid_engine` | Pure calculation — grid levels, spacing, anchor logic (no side effects) |
| `order_manager` | Batch order ops, reconciliation, flatten protocol, backstop stop-loss |
| `risk_manager` | Inventory limits, breakout detection, vol circuit breaker, drawdown guards |
| `pnl_monitor` | PnL tracking, funding accrual, cross-check against exchange |
| `state_store` | SQLite persistence for crash recovery |

See `docs/design.md` for the full design, and `docs/architecture.md` for the module dependency graph.

## Setup

**Requirements:** Python 3.11+, a Hyperliquid account with API credentials.

```bash
pip install -e .
cp config/gridbot.example.yaml config/gridbot.yaml
# Edit gridbot.yaml — set your credentials, start on testnet: true
```

## Running

```bash
# Testnet (default config has testnet: true)
python -m gridbot --config config/gridbot.yaml

# Dry-run (no orders placed)
python -m gridbot --config config/gridbot.yaml --dry-run
```

## Configuration

Copy `config/gridbot.example.yaml` and adjust. Key parameters:

| Parameter | BTC default | ETH default |
|---|---|---|
| `leverage` | 2.0× | 2.0× |
| `levels_per_side` | 25 | 25 |
| `capital_allocation` | 60% | 40% |
| `breakout_atr_distance` | 4.5 ATR | 4.0 ATR |
| `max_flatten_slippage_bps` | 50 bps | 75 bps |

See `docs/design.md` sections 11.1–11.3 for the full parameter reference.

## Testing

```bash
pytest tests/
```

Unit test coverage: GridEngine 97%, RiskManager 96%, MarketData 72%, PnLMonitor 100%, StateStore 96%. Integration tests in `tests/test_integration.py`.

## Docs

| Doc | Contents |
|---|---|
| `docs/design.md` | Authoritative design reference — architecture, risk model, parameters |
| `docs/architecture.md` | Module dependency graph and data flow |
| `docs/risk-model.md` | Risk model implementation detail |
| `docs/operations.md` | Deployment, testnet soak, operational procedures |

## Warnings

- Not a set-and-forget system. Requires monitoring and periodic parameter review.
- Start on testnet. Validate behavior through the full soak procedure in `docs/operations.md` before switching to mainnet.
- Grid trading accumulates inventory in trending markets. The risk model limits but does not eliminate this exposure.
