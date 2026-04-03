# Progress Log

## 2026-04-03 — Phase 1 review fixes

**Goal:** Fix issues found during Phase 1 code review.

**Changes:**
- Config hash now uses stable `grid_config.range_atr` and `grid_config.step_bps` instead of dynamically-computed effective step — prevents order churn from spread/vol fluctuations
- `compute_effective_step` now takes `mid_price` and computes `ATR / mid_price * 10_000` directly per design doc section 5.4, instead of using realized_vol as a proxy
- Soft-cap inventory skew clamps unwind-side boost to `max_abs_position / levels_per_side` to prevent oversized orders
- Removed dead `_did_reanchor` flag (was never set to True; Supervisor will own re-anchor state)
- Added test for expansion sizing using `expansion_allocation` vs `capital_allocation`
- Added test for soft-cap unwind size clamp
- Fixed test comments referencing old `capital_allocation=0.60` (now 0.70)

**Files modified:**
- `gridbot/grid_engine.py` — config hash fix, effective step formula, skew clamp, dead code removal
- `tests/test_grid_engine.py` — updated grid spacing tests for new formula, added expansion sizing and skew clamp tests

**Status:** Complete

**Notes:** Pending flips at HARD_CAP on the exposure-increasing side are marked reduce_only per design doc section 7.6. These will be rejected by the exchange (e.g., reduce-only buy when long). This is correct per spec (exchange as safety net) but will produce rejected orders each cycle. Flagged for future consideration.

---

## 2026-04-02 — Phase 1: GridEngine implementation

**Goal:** Implement all GridEngine methods per implementation plan Phase 1, with comprehensive tests.

**Changes:**
- Implemented all 9 GridEngine sub-tasks: grid spacing formula (5.4/5.7), order sizing (5.5), inventory classification and skewing (6.2), core grid levels (5.2 L1), expansion grid levels (5.2 L2), staggered placement (5.3), anchor management (5.1), pending flips (7.6), and compute_desired_orders orchestration (7.1)
- Constructor now takes both AssetConfig and OperationalConfig (needed for stagger_initial_levels)
- Added `_compute_expansion_order_size` helper for expansion layer sizing via expansion_allocation fraction
- Added baseline_vol field to VolMetrics (needed for dynamic slippage buffer formula)
- Added account_equity, stagger_placed_count, drift_start_ms, anchor_epoch fields to AssetState
- Wrote 77 tests covering all methods; 97% line coverage on grid_engine.py

**Files modified:**
- `gridbot/grid_engine.py` — Full implementation replacing all NotImplementedError stubs
- `gridbot/types.py` — Added baseline_vol to VolMetrics; added account_equity, stagger_placed_count, drift_start_ms, anchor_epoch to AssetState
- `tests/test_grid_engine.py` — Comprehensive test suite (77 tests)
- `docs/plans/implementation-plan.md` — Marked Phase 1 and all sub-headers [DONE]
- `REPO_MAP.md` — Updated test description
- `progress.md` — This entry

**Status:** Complete

**Notes:** GridEngine constructor signature changed from `(AssetConfig)` to `(AssetConfig, OperationalConfig)` — Supervisor will need to pass both when instantiating. The ATR-to-step mapping uses a linear interpolation between grid_step_bps_min/max based on vol_ratio to baseline; this is a reasonable heuristic that can be refined during testnet soak if the design doc provides a more specific formula.

---

## 2026-04-01 — Implementation plan review and corrections

**Goal:** Review the implementation plan against the design doc and fix identified issues.

**Changes:**
- Phase 1.9: Added note that GridEngine receives regime via state, Phase 1 tests must set `state.regime` explicitly
- Phase 1.5: Added inline note clarifying distinction between `expansion_range_atr` (level extent) and `breakout_atr_distance` (activation upper bound), including the 0.5 ATR buffer zone
- Phase 1.2: Replaced "confirm on testnet" with placeholder constants (validated in Phase 8)
- Phase 2.6: Clarified that equity for drawdown checks comes from exchange-reported account equity fetched by Supervisor; renamed `_pnl_history` to `_equity_history` with `record_equity()` API
- Phase 2.9: Changed `pos.price` to `pos.mark_price` in portfolio delta formula with rationale
- Phase 3.1: Added `schema_version` table and migration strategy to StateStore schema
- Phase 4.1: Replaced `allMids` with `l2Book` as primary WS subscription for bid/ask/spread; `allMids` demoted to fallback
- Phase 5.1: Specified average-cost method for PnL computation (matches Hyperliquid reporting), added position-flip case
- Phase 6.3: Added partial batch failure handling — HL batches are not transactional; per-order response parsing, local state reconciliation on partial failure
- Phase 7.4: Decoupled main loop tick (`cycle_interval_seconds`) from REST reconciliation timer (`reconcile_interval_seconds`)
- Phase 7.5: Expanded from 8-step to 10-step cycle — added exchange equity fetch before risk eval (step 1), backstop update after grid reconciliation (step 6), REST reconciliation on separate timer (step 9)
- Phase 2.1: Added vol history bootstrap strategy — 48h minimum window expanding to 7d, with conservative bias (tightened percentile thresholds) during bootstrap period

**Files modified:**
- `docs/plans/implementation-plan.md` — All corrections above

**Status:** Complete

---

## 2026-03-30 — Implementation plan

**Goal:** Create a structured end-to-end implementation plan covering all modules from stub to testnet deployment.

**Changes:**
- Wrote 8-phase implementation plan with subtasks, test requirements, and acceptance criteria per phase
- Phases ordered by dependency: pure logic first (GridEngine, RiskManager), then infrastructure (StateStore, MarketData), then exchange ops (OrderManager, PnLMonitor), then orchestration (Supervisor), then integration

**Files added:**
- `docs/plans/implementation-plan.md` — Master implementation plan

**Files modified:**
- `REPO_MAP.md` — Added docs/plans/ section
- `progress.md` — This entry

**Status:** Complete

---

## 2026-03-30 — Code and documentation scaffolding

**Goal:** Set up the full project structure with module stubs, types, configuration, tests, and documentation — ready for implementation.

**Changes:**
- Created Python package (`gridbot/`) with all six core modules as stubs
- Defined shared types: enums (Regime, BotState, OrderSide, GridLayer, InventoryZone, TimeInForce) and dataclasses (GridLevel, DesiredOrder, OpenOrder, Fill, Position, PendingFlip, GridConfig, VolMetrics, AssetState)
- Built configuration system with per-asset defaults matching design doc sections 11.1-11.3, YAML loading with override support
- Scaffolded all module classes with method signatures and docstrings referencing design doc sections
- Created test structure with initial tests for deterministic order IDs and config defaults
- Added example YAML config and three implementation-focused docs (architecture, risk-model, operations)
- Set up pyproject.toml with dependencies, dev tools (pytest, ruff, mypy), and CLI entry point

**Files added:**
- `pyproject.toml` — Project config, dependencies, tooling
- `config/gridbot.example.yaml` — Example configuration
- `gridbot/__init__.py` — Package init
- `gridbot/config.py` — Configuration dataclasses and YAML loader
- `gridbot/grid_engine.py` — GridEngine stub (pure calculation)
- `gridbot/main.py` — CLI entry point
- `gridbot/market_data.py` — MarketData stub (WS/REST)
- `gridbot/order_manager.py` — OrderManager stub (batch order ops)
- `gridbot/pnl_monitor.py` — PnLMonitor stub (analytics)
- `gridbot/risk_manager.py` — RiskManager stub (safety constraints)
- `gridbot/state_store.py` — StateStore stub (SQLite persistence)
- `gridbot/supervisor.py` — Supervisor stub (orchestrator)
- `gridbot/types.py` — Shared enums and dataclasses
- `tests/__init__.py` — Test package init
- `tests/test_config.py` — Config default tests
- `tests/test_grid_engine.py` — Order ID and config hash tests
- `tests/test_order_manager.py` — Order diff test placeholders
- `tests/test_risk_manager.py` — Risk check test placeholders
- `docs/architecture.md` — Module dependency and data flow guide
- `docs/operations.md` — Deployment and operations guide
- `docs/risk-model.md` — Risk model implementation guide

**Files modified:**
- `REPO_MAP.md` — Updated with all new files
- `progress.md` — This entry

**Status:** Complete

**Notes:** All module methods raise `NotImplementedError` — they are stubs with correct signatures and docstrings. Implementation order recommendation: types/config (done) -> GridEngine (pure, testable) -> RiskManager -> MarketData -> StateStore -> OrderManager -> PnLMonitor -> Supervisor.

---

## 2026-03-30 — Project scaffolding and dev guidelines

**Goal:** Establish development guardrails, tracking files, and documentation standards.

**Changes:**
- Created CLAUDE.md with core invariants, anti-patterns, and AI behavior rules
- Added REPO_MAP.md for file tracking
- Added progress.md for structured development logging
- Added documentation maintenance guidance to CLAUDE.md

**Files added:**
- `CLAUDE.md` — AI development guidelines, project invariants, tracking rules
- `REPO_MAP.md` — File index
- `progress.md` — Development log (this file)

**Status:** Complete

---
