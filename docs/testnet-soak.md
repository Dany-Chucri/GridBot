# Testnet Soak Procedure

> **Phase:** 8.3 of [implementation-plan.md](plans/implementation-plan.md).
> **Goal:** 48-72 hours of continuous testnet operation with no manual intervention.
> **Authoritative reference:** [design.md](design.md) section 10.5.

---

## Pre-deploy checklist

Before provisioning the VPS:

- [ ] Full test suite green locally: `pytest`.
- [ ] `docs/design.md` reviewed; no outstanding implementation divergences flagged in `REPO_MAP.md` / `progress.md`.
- [ ] Master Hyperliquid testnet wallet funded with USDC via the faucet (> 2x the configured capital allocation for headroom).
- [ ] An API/agent wallet registered under the master account (app.hyperliquid-testnet.xyz/API or mainnet equivalent) — the bot trades via this delegated key, which can sign orders but never withdraw funds. Its private key exported to a **secure** password manager entry, never committed, never emailed.
- [ ] `gridbot.yaml`'s `wallet_address` set to the **master** account's address (not the agent wallet's address) — see `deploy/gridbot.env.example` for how the two relate.
- [ ] At least one alert channel enabled under `alerting:` in `gridbot.yaml` (Telegram and/or Discord), with the matching secret set in `gridbot.env` — see [operations.md](operations.md#alerting). A multi-day unattended soak with no alert channel means a kill switch or flatten failure sits silently until someone tails the log.

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

# 5. Install log rotation (the service appends directly to
#    /var/log/gridbot/*.log with no rotation of its own — a multi-day soak
#    at --log-level INFO will otherwise grow unbounded)
sudo cp deploy/gridbot.logrotate /etc/logrotate.d/gridbot
```

`--log-level INFO` (the service default) logs startup/shutdown, regime transitions, order batches, fills, risk events, and reconcile discrepancies — the design doc 10.2 minimum set. The per-cycle heartbeat line (`cycle symbol=... regime=... mid=...`) is DEBUG-only; pass `--log-level DEBUG` (edit `ExecStart` in the service file) only for short diagnostic runs, not for the multi-day soak — at ~1 line/s/asset it dominates log volume and isn't needed to judge pass/fail.

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
- [ ] `save_bot_state` succeeds (implicit — no exceptions from StateStore).
- [ ] `regime transition symbol=... UNKNOWN -> ...` logged once vol history clears the 48h bootstrap minimum (section 6.4's bootstrap gate), and again on any later RANGE ↔ TREND / HIGH_VOL change. Regime stays `UNKNOWN` (no grid placed) until then — expected, not a bug.
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

## Recovering from a DEAD state

A `KILL` switch trip is sticky by design (design doc §6.6: "the bot enters a dead state that requires human intervention to restart"). `Supervisor._recover_state` explicitly preserves `bot_state=DEAD` across process restarts, and pre-flight refuses to promote a `DEAD` asset back to `RUNNING` — so a plain restart (or `systemctl restart`) after a kill will reconnect, recover state, immediately see `DEAD`, and exit again. This is correct: it forces an operator to actually look at why it died before trading resumes. There is deliberately no automated reset path.

Once you've reviewed the cause and decided it's safe to resume:

- **Dev/smoke-testing iteration** (no position, no fill history worth keeping): stop the bot and delete the local state DB — `rm data/gridbot.db*` — for a fully clean slate on the next run.
- **Preserving history on a real soak**: clear just the stuck symbols' `bot_state` back to `STARTING` via SQL, e.g. `sqlite3 /opt/gridbot/data/gridbot.db "UPDATE bot_state SET state_json = json_set(state_json, '$.bot_state', 'STARTING') WHERE symbol = 'BTC-PERP';"` (repeat per symbol). Do this only after confirming via the exchange UI that there's no unexpected residual position, and only ever manually — never scripted into the restart path.

## Post-soak handoff

Once the window completes, run `scripts/post_soak_analysis.py` against the persisted SQLite DB and attach the report to the PR promoting mainnet readiness (see `docs/operations.md` testing progression).
