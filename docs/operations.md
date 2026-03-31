# Operations Guide

> Deployment and operational procedures. See [design.md](design.md) section 10 for full specification.

## Quick Start

```bash
# 1. Copy and edit config
cp config/gridbot.example.yaml config/gridbot.yaml

# 2. Install
pip install -e ".[dev]"

# 3. Run on testnet
gridbot --testnet --config config/gridbot.yaml

# 4. Run tests
pytest
```

## Testing Progression

Follow the phases defined in [design.md](design.md) section 10.5:

| Phase | Duration | Key Criteria |
|---|---|---|
| Testnet soak | 48-72h | No kill switches, no state desync |
| Mainnet tiny | 1-2 weeks | Min sizes, BTC only |
| Mainnet small | 2-4 weeks | Small sizes, both assets |
| Mainnet target | Ongoing | Full parameters, gradual scale-up |

## Alerting

Alerts fire on events defined in [design.md](design.md) section 10.3. Configure the alert channel (Telegram/Discord/email) in `config/gridbot.yaml`.

## Shutdown Behavior

- **Graceful (SIGTERM/SIGINT)**: cancels orders, persists state, exits. Does NOT flatten.
- **Hard kill (SIGKILL/crash)**: restart sequence reconciles against exchange state automatically.

See [design.md](design.md) sections 4.4-4.5.

## Logging

Structured logs with UTC timestamps, millisecond precision. Every regime transition, order batch, fill, risk event, and reconciliation discrepancy is logged. See [design.md](design.md) section 10.2.
