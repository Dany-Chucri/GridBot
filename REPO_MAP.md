# Repo Map

## root
- `README.md` — Project overview
- `REPO_MAP.md` — File index (this file)
- `progress.md` — Development log
- `pyproject.toml` — Python project config, dependencies, tool settings

## config/
- `gridbot.example.yaml` — Example configuration (copy to gridbot.yaml)

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

## tests/
- `test_config.py` — Configuration defaults and loading
- `test_grid_engine.py` — Full GridEngine test suite (80 tests, 97% coverage)
- `test_order_manager.py` — Order diff, flip logic, ALO rejection (stubs)
- `test_risk_manager.py` — Full RiskManager test suite (59 tests, 96% coverage)
