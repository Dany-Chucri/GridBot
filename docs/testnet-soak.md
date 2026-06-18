# Testnet Soak Procedure

> **Phase:** 8.3 of [implementation-plan.md](plans/implementation-plan.md).
> **Goal:** 48-72 hours of continuous testnet operation with no manual intervention.
> **Authoritative reference:** [design.md](design.md) section 10.5.

---

## Pre-deploy checklist

Before provisioning the VPS:

- [ ] Full test suite green locally: `pytest`.
- [ ] `docs/design.md` reviewed; no outstanding implementation divergences flagged in `REPO_MAP.md` / `progress.md`.
- [ ] Hyperliquid testnet wallet funded with USDC (> 2x the configured capital allocation for headroom).
- [ ] Wallet private key exported to a **secure** password manager entry. Never committed, never emailed.

## VPS provisioning

Any small cloud instance is fine — the bot is I/O-bound, not CPU-heavy. Debian 12 / Ubuntu 22.04 assumed below.

```bash
# 1. Create a dedicated user
sudo adduser --system --group --home /opt/gridbot gridbot

# 2. Clone and install
sudo -u gridbot git clone <REPO> /opt/gridbot
cd /opt/gridbot
sudo -u gridbot python3.11 -m venv .venv
sudo -u gridbot .venv/bin/pip install -e .

# 3. Config + secrets
sudo mkdir -p /etc/gridbot /var/log/gridbot /opt/gridbot/data
sudo cp deploy/gridbot.testnet.yaml /etc/gridbot/gridbot.yaml
sudo cp deploy/gridbot.env.example /etc/gridbot/gridbot.env
sudo chown root:gridbot /etc/gridbot/gridbot.env
sudo chmod 640 /etc/gridbot/gridbot.env
sudo chown gridbot:gridbot /var/log/gridbot /opt/gridbot/data
# Edit /etc/gridbot/gridbot.env and paste the real GRIDBOT_PRIVATE_KEY

# 4. Install the service
sudo cp deploy/gridbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gridbot
sudo systemctl status gridbot
```

## Live monitoring checklist

Tail the logs during the first hour:

```bash
sudo journalctl -u gridbot -f
# or
sudo tail -f /var/log/gridbot/gridbot.log
```

Expected log events (design doc 10.2 / 10.3):

- [ ] `Initialization complete` within 30s of start.
- [ ] `Pre-flight checks passed` with a positive equity.
- [ ] Periodic `cycle symbol=BTC-PERP regime=... mid=...` entries at ~1/s cadence.
- [ ] `save_bot_state` succeeds (implicit — no exceptions from StateStore).
- [ ] Regime transitions logged when conditions change (RANGE ↔ TREND / HIGH_VOL).
- [ ] Fills routed through the fill pump (look for PnLMonitor updates).
- [ ] REST reconciliation runs on its own cadence (5s default).

## Pass/fail criteria (48-72h window)

Pass only if **all** of the following hold:

- [ ] No `KILL` switch events (`BotState.DEAD` never entered from a bot-originated error).
- [ ] No unrecoverable state desync (REST reconciliation always succeeds within `max_time_desynced_seconds`).
- [ ] No PnL cross-check divergence above `pnl_divergence_threshold_usd`.
- [ ] State survives at least one process restart (kill the process, confirm `_recover_state` restores cleanly).
- [ ] Maintenance windows (if any) cleanly enter / exit `MAINTENANCE` without error count accumulation.
- [ ] All resting orders are `ALO` post-only; IOC only observed during a (simulated or real) flatten.
- [ ] Backstop stop-loss orders exist whenever a non-zero position exists.

## Simulated-failure drills (optional but recommended)

Run these to validate each recovery path within the soak window:

1. **Hard kill the process** (`sudo systemctl kill -s SIGKILL gridbot`). systemd will restart. Confirm state recovery and orphan cleanup in the logs.
2. **Revoke network briefly** (`sudo iptables -A OUTPUT -d <hyperliquid-host> -j DROP`; revert after 30s). Confirm the bot enters `MAINTENANCE` and exits cleanly on reconnect.
3. **Restart during a position hold**. Open a position (let the grid fill), hard-restart, confirm position is adopted from exchange and grid rebuilds around it.

## Post-soak handoff

Once the window completes, run `scripts/post_soak_analysis.py` against the persisted SQLite DB and attach the report to the PR promoting mainnet readiness (see `docs/operations.md` testing progression).
