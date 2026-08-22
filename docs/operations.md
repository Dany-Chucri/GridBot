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
| Testnet soak | 48-72h | No kill switches, no state desync. Procedure: [testnet-soak.md](testnet-soak.md) |
| Mainnet tiny | 1-2 weeks | Min sizes, BTC only |
| Mainnet small | 2-4 weeks | Small sizes, both assets |
| Mainnet target | Ongoing | Full parameters, gradual scale-up |

## Deployment

VPS deployment uses the systemd unit at [`deploy/gridbot.service`](../deploy/gridbot.service) and the testnet config template at [`deploy/gridbot.testnet.yaml`](../deploy/gridbot.testnet.yaml). Full provisioning steps are in [testnet-soak.md](testnet-soak.md).

## Post-Soak Analysis

After the 48-72h window, run `scripts/post_soak_analysis.py` against the persisted SQLite DB to produce a summary report: fill profitability after fees, grid-step regime distribution, backstop coverage, flatten events, PnL reconciliation. Attach the report when promoting to the next testing phase.

## Alerting

Alerts fire on events defined in [design.md](design.md) section 10.3, delivered via `gridbot/alerting.py`. Enable Telegram and/or Discord under `alerting:` in `config/gridbot.yaml` (channel toggles, chat ID, severity filter, see `config/gridbot.example.yaml`); the corresponding secret (`GRIDBOT_TELEGRAM_BOT_TOKEN` / `GRIDBOT_DISCORD_WEBHOOK_URL`) must be set in the environment (see `deploy/gridbot.env.example`) or that channel is silently skipped with a startup warning. No channel enabled means alerts only reach the log file, fine for local dev, not for an unattended soak.

**Verify delivery before a soak:** `python scripts/test_alert.py [--config path]` loads config the same way the bot does and fires one real message at each severity the bot uses (INFO, WARNING, CRITICAL), printing whether each is expected to clear `alerting.min_severity`. Confirm the messages that should arrive actually do (default `min_severity: WARNING` means INFO is expected to be suppressed). Delivery failures (bad token, wrong chat ID, revoked webhook) show as an `ERROR ... alert failed` log line, not an exception, check for that line, don't just trust a clean exit. Pass `--severity` to send just one.

## Shutdown Behavior

- **Graceful (SIGTERM/SIGINT)**: cancels orders, persists state, exits. Does NOT flatten.
- **Hard kill (SIGKILL/crash)**: restart sequence reconciles against exchange state automatically.

See [design.md](design.md) sections 4.4-4.5.

## Logging

Structured logs with UTC timestamps, millisecond precision. Every regime transition, order batch, fill, risk event, and reconciliation discrepancy is logged. See [design.md](design.md) section 10.2.
