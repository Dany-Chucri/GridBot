# Repo Map

## root
- `README.md` — Project overview
- `REPO_MAP.md` — File index (this file)
- `progress.md` — Development log
- `pyproject.toml` — Python project config, dependencies, tool settings
- `requirements-dev.txt` — Dev/test deps for `pip install -r`, mirrors pyproject `[dev]` extra
- `requirements.txt` — Runtime deps for `pip install -r`, mirrors pyproject dependencies

## config/
- `gridbot.example.yaml` — Example configuration (copy to gridbot.yaml)

## deploy/
- `gridbot.env.example` — Env var template (agent wallet private key, secrets)
- `gridbot.logrotate` — logrotate policy for `/var/log/gridbot/*.log` (daily, 14-day retention, copytruncate)
- `gridbot.service` — systemd unit for VPS deployment
- `gridbot.testnet.yaml` — Testnet deployment config

## docs/
- `architecture.md` — Module dependency graph and data flow guide
- `design.md` — Authoritative design document (architecture, risk model, parameters)
- `operations.md` — Deployment, testing progression, and operational procedures
- `research.md` — Background research and references
- `risk-model.md` — Risk model implementation guide

## docs/plans/
- `implementation-plan.md` — End-to-end 8-phase implementation plan with acceptance criteria

## gridbot/
- `__init__.py` — Package init, version
- `alerting.py` — Telegram/Discord alert delivery, wired into Supervisor's alert callback
- `config.py` — Configuration loading and parameter dataclasses
- `grid_engine.py` — Pure calculation: grid levels, spacing, sizing, anchor logic
- `main.py` — CLI entry point, argument parsing, signal handling
- `market_data.py` — WS/REST market data, fills, vol metrics
- `order_manager.py` — Batch order ops, reconciliation, flatten protocol, backstop
- `pnl_monitor.py` — PnL tracking, funding, exchange cross-check
- `risk_manager.py` — Risk constraints, regime detection, pre-flight checks
- `state_store.py` — SQLite persistence for crash recovery
- `supervisor.py` — Main event loop, module orchestration, shutdown
- `types.py` — Shared enums, dataclasses, type definitions

## scripts/
- `post_soak_analysis.py` — Post-soak SQLite report generator (see docs/testnet-soak.md)
- `test_alert.py` — Fires one real alert through the configured channel(s) to verify delivery end-to-end

## tests/
- `test_alerting.py` — Alert channel build/dispatch tests (Telegram, Discord, severity filtering)
- `test_config.py` — Configuration defaults and loading
- `test_grid_engine.py` — Full GridEngine test suite (80 tests, 97% coverage)
- `test_market_data.py` — Full MarketData test suite (102 tests, 72% coverage)
- `test_order_manager.py` — Full OrderManager test suite (117 tests)
- `test_pnl_monitor.py` — Full PnLMonitor test suite (28 tests, 100% coverage)
- `test_risk_manager.py` — Full RiskManager test suite (64 tests, 96% coverage)
- `test_state_store.py` — Full StateStore test suite (52 tests, 96% coverage)
- `test_supervisor.py` — Supervisor orchestration suite (28 tests, mocked modules)
