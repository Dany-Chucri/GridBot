"""StateStore — persistent storage via SQLite.

Responsibilities (design doc section 3.2):
- Store bot config version, grid spec, regime, positions, order map, fills
- All writes are transactional
- On crash recovery, state is consistent up to the last committed transaction

Persisted data (section 4.4):
- Bot config version + all parameters
- Current anchor price, range, step, active levels
- Current regime + regime transition timestamp
- Last known position + average entry price
- Order map: level_price -> order_id -> status -> remaining_qty
- Fills ledger
- Last heartbeat / last successful reconciliation time
- Cooldown state + cooldown start time
- Pending flips set (section 7.6)
- FLATTENING state (section 6.7)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from gridbot.types import (
    AssetState,
    BotState,
    Fill,
    GridConfig,
    InventoryZone,
    OpenOrder,
    OrderSide,
    PendingFlip,
    Position,
    Regime,
    TimeInForce,
    VolMetrics,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/gridbot.db")

CURRENT_SCHEMA_VERSION = 1

# SQL for schema v1
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS grid_config (
    symbol TEXT PRIMARY KEY,
    anchor REAL NOT NULL,
    range_atr REAL NOT NULL,
    step_bps REAL NOT NULL,
    epoch INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    size REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    liq_price REAL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS open_orders (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    order_id INTEGER NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    remaining REAL NOT NULL,
    side TEXT NOT NULL,
    reduce_only INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    side TEXT NOT NULL,
    fee REAL NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    is_maker INTEGER NOT NULL,
    is_partial INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS regime (
    symbol TEXT PRIMARY KEY,
    regime TEXT NOT NULL,
    transition_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_flips (
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    originating_fill_id TEXT NOT NULL,
    PRIMARY KEY (symbol, originating_fill_id)
);

CREATE TABLE IF NOT EXISTS bot_state (
    symbol TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    symbol TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, timestamp_ms);
"""


def _compute_config_hash(config: GridConfig) -> str:
    """Deterministic hash of grid config for change detection."""
    raw = f"{config.symbol}:{config.anchor}:{config.range_atr}:{config.step_bps}:{config.epoch}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _serialize_asset_state(state: AssetState) -> str:
    """Serialize AssetState to JSON, converting enums to their names."""
    d = asdict(state)
    # Convert enum values to serializable form
    d["regime"] = state.regime.name
    d["bot_state"] = state.bot_state.name
    if state.position is not None:
        d["position"]["symbol"] = state.position.symbol
    if state.vol_metrics is not None:
        pass  # all numeric, asdict handles it
    if state.grid_config is not None:
        pass  # all numeric/str, asdict handles it
    # Convert OpenOrder list — side enums
    d["open_orders"] = [
        {**asdict(o), "side": o.side.value} for o in state.open_orders
    ]
    # Convert PendingFlip list — side enums
    d["pending_flips"] = [
        {**asdict(f), "side": f.side.value} for f in state.pending_flips
    ]
    return json.dumps(d)


def _deserialize_asset_state(raw: str) -> AssetState:
    """Deserialize JSON back to AssetState."""
    d = json.loads(raw)
    pos = None
    if d.get("position") is not None:
        pos = Position(**d["position"])
    vol = None
    if d.get("vol_metrics") is not None:
        vol = VolMetrics(**d["vol_metrics"])
    gc = None
    if d.get("grid_config") is not None:
        gc = GridConfig(**d["grid_config"])
    open_orders = [
        OpenOrder(
            order_id=o["order_id"],
            client_order_id=o["client_order_id"],
            symbol=o["symbol"],
            price=o["price"],
            size=o["size"],
            remaining=o["remaining"],
            side=OrderSide(o["side"]),
            reduce_only=o["reduce_only"],
        )
        for o in d.get("open_orders", [])
    ]
    pending_flips = [
        PendingFlip(
            price=f["price"],
            side=OrderSide(f["side"]),
            size=f["size"],
            originating_fill_id=f["originating_fill_id"],
        )
        for f in d.get("pending_flips", [])
    ]
    return AssetState(
        symbol=d["symbol"],
        mid_price=d.get("mid_price", 0.0),
        mark_price=d.get("mark_price", 0.0),
        regime=Regime[d["regime"]],
        bot_state=BotState[d["bot_state"]],
        position=pos,
        vol_metrics=vol,
        grid_config=gc,
        open_orders=open_orders,
        pending_flips=pending_flips,
        cooldown_until_ms=d.get("cooldown_until_ms"),
        account_equity=d.get("account_equity", 0.0),
        funding_rate=d.get("funding_rate", 0.0),
        moving_avg=d.get("moving_avg", 0.0),
        last_breakout_ms=d.get("last_breakout_ms"),
        stagger_placed_count=d.get("stagger_placed_count", 0),
        drift_start_ms=d.get("drift_start_ms"),
        anchor_epoch=d.get("anchor_epoch", 0),
    )


class StateStore:
    """SQLite-backed persistent state for crash recovery and analytics."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open DB connection and create tables if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        # Apply schema migrations
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        """Check current schema version and apply any pending migrations."""
        # Check if schema_version table exists
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        exists = await cursor.fetchone()

        if not exists:
            # Fresh database — apply v1 schema
            await self._conn.executescript(_SCHEMA_V1)
            await self._conn.execute(
                "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
                (1, _now_ms()),
            )
            await self._conn.commit()
            logger.info("Applied schema v1 (fresh database)")
            return

        # Get current version
        cursor = await self._conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        )
        row = await cursor.fetchone()
        current_version = row["v"] if row else 0

        # Apply pending migrations sequentially
        migrations = {
            # Future: 1: self._migrate_v1_to_v2,
        }
        for version in range(current_version, CURRENT_SCHEMA_VERSION):
            migrate_fn = migrations.get(version)
            if migrate_fn:
                await migrate_fn()
                await self._conn.execute(
                    "INSERT INTO schema_version (version, applied_ms) VALUES (?, ?)",
                    (version + 1, _now_ms()),
                )
                await self._conn.commit()
                logger.info("Applied migration v%d -> v%d", version, version + 1)

    async def close(self) -> None:
        """Close DB connection."""
        if self._conn is not None:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Grid config persistence
    # ------------------------------------------------------------------

    async def save_grid_config(self, config: GridConfig) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO grid_config
               (symbol, anchor, range_atr, step_bps, epoch, config_hash, updated_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                config.symbol,
                config.anchor,
                config.range_atr,
                config.step_bps,
                config.epoch,
                _compute_config_hash(config),
                _now_ms(),
            ),
        )
        await self._conn.commit()

    async def load_grid_config(self, symbol: str) -> GridConfig | None:
        cursor = await self._conn.execute(
            "SELECT symbol, anchor, range_atr, step_bps, epoch FROM grid_config WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return GridConfig(
            symbol=row["symbol"],
            anchor=row["anchor"],
            range_atr=row["range_atr"],
            step_bps=row["step_bps"],
            epoch=row["epoch"],
        )

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    async def save_position(self, position: Position) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, size, avg_entry_price, unrealized_pnl, liq_price, updated_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                position.symbol,
                position.size,
                position.avg_entry_price,
                position.unrealized_pnl,
                position.liquidation_price,
                _now_ms(),
            ),
        )
        await self._conn.commit()

    async def load_position(self, symbol: str) -> Position | None:
        cursor = await self._conn.execute(
            "SELECT symbol, size, avg_entry_price, unrealized_pnl, liq_price FROM positions WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Position(
            symbol=row["symbol"],
            size=row["size"],
            avg_entry_price=row["avg_entry_price"],
            unrealized_pnl=row["unrealized_pnl"],
            liquidation_price=row["liq_price"],
        )

    # ------------------------------------------------------------------
    # Order map persistence
    # ------------------------------------------------------------------

    async def save_open_orders(self, symbol: str, orders: list[OpenOrder]) -> None:
        await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                "DELETE FROM open_orders WHERE symbol = ?", (symbol,)
            )
            for order in orders:
                await self._conn.execute(
                    """INSERT INTO open_orders
                       (client_order_id, symbol, order_id, price, size, remaining, side, reduce_only)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        order.client_order_id,
                        order.symbol,
                        order.order_id,
                        order.price,
                        order.size,
                        order.remaining,
                        order.side.value,
                        int(order.reduce_only),
                    ),
                )
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise

    async def load_open_orders(self, symbol: str) -> list[OpenOrder]:
        cursor = await self._conn.execute(
            "SELECT * FROM open_orders WHERE symbol = ?", (symbol,)
        )
        rows = await cursor.fetchall()
        return [
            OpenOrder(
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                symbol=row["symbol"],
                price=row["price"],
                size=row["size"],
                remaining=row["remaining"],
                side=OrderSide(row["side"]),
                reduce_only=bool(row["reduce_only"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Fills ledger
    # ------------------------------------------------------------------

    async def record_fill(self, fill: Fill) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO fills
               (fill_id, order_id, client_order_id, symbol, price, size, side, fee, timestamp_ms, is_maker, is_partial)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill.fill_id,
                fill.order_id,
                fill.client_order_id,
                fill.symbol,
                fill.price,
                fill.size,
                fill.side.value,
                fill.fee,
                fill.timestamp_ms,
                int(fill.is_maker),
                int(fill.is_partial),
            ),
        )
        await self._conn.commit()

    async def get_fills(self, symbol: str, since_ms: int | None = None) -> list[Fill]:
        if since_ms is not None:
            cursor = await self._conn.execute(
                "SELECT * FROM fills WHERE symbol = ? AND timestamp_ms >= ? ORDER BY timestamp_ms",
                (symbol, since_ms),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM fills WHERE symbol = ? ORDER BY timestamp_ms",
                (symbol,),
            )
        rows = await cursor.fetchall()
        return [
            Fill(
                fill_id=row["fill_id"],
                order_id=row["order_id"],
                client_order_id=row["client_order_id"],
                symbol=row["symbol"],
                price=row["price"],
                size=row["size"],
                side=OrderSide(row["side"]),
                fee=row["fee"],
                timestamp_ms=row["timestamp_ms"],
                is_maker=bool(row["is_maker"]),
                is_partial=bool(row["is_partial"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Regime persistence
    # ------------------------------------------------------------------

    async def save_regime(self, symbol: str, regime: Regime, timestamp_ms: int) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO regime (symbol, regime, transition_ms)
               VALUES (?, ?, ?)""",
            (symbol, regime.name, timestamp_ms),
        )
        await self._conn.commit()

    async def load_regime(self, symbol: str) -> tuple[Regime, int] | None:
        """Returns (regime, transition_timestamp_ms) or None."""
        cursor = await self._conn.execute(
            "SELECT regime, transition_ms FROM regime WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return (Regime[row["regime"]], row["transition_ms"])

    # ------------------------------------------------------------------
    # Pending flips (section 7.6)
    # ------------------------------------------------------------------

    async def save_pending_flips(self, symbol: str, flips: list[PendingFlip]) -> None:
        await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                "DELETE FROM pending_flips WHERE symbol = ?", (symbol,)
            )
            for flip in flips:
                await self._conn.execute(
                    """INSERT INTO pending_flips
                       (symbol, price, side, size, originating_fill_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (symbol, flip.price, flip.side.value, flip.size, flip.originating_fill_id),
                )
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise

    async def load_pending_flips(self, symbol: str) -> list[PendingFlip]:
        cursor = await self._conn.execute(
            "SELECT price, side, size, originating_fill_id FROM pending_flips WHERE symbol = ?",
            (symbol,),
        )
        rows = await cursor.fetchall()
        return [
            PendingFlip(
                price=row["price"],
                side=OrderSide(row["side"]),
                size=row["size"],
                originating_fill_id=row["originating_fill_id"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Bot state & heartbeat
    # ------------------------------------------------------------------

    async def save_bot_state(self, symbol: str, state: AssetState) -> None:
        """Persist full asset state snapshot (for restart recovery)."""
        await self._conn.execute(
            """INSERT OR REPLACE INTO bot_state (symbol, state_json, updated_ms)
               VALUES (?, ?, ?)""",
            (symbol, _serialize_asset_state(state), _now_ms()),
        )
        await self._conn.commit()

    async def load_bot_state(self, symbol: str) -> AssetState | None:
        cursor = await self._conn.execute(
            "SELECT state_json FROM bot_state WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _deserialize_asset_state(row["state_json"])

    async def update_heartbeat(self, symbol: str, timestamp_ms: int) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO heartbeat (symbol, timestamp_ms)
               VALUES (?, ?)""",
            (symbol, timestamp_ms),
        )
        await self._conn.commit()

    async def get_last_heartbeat(self, symbol: str) -> int | None:
        cursor = await self._conn.execute(
            "SELECT timestamp_ms FROM heartbeat WHERE symbol = ?",
            (symbol,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row["timestamp_ms"]
