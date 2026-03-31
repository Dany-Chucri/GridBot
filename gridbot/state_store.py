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

import logging
from pathlib import Path

from gridbot.types import (
    AssetState,
    Fill,
    GridConfig,
    OpenOrder,
    PendingFlip,
    Position,
    Regime,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/gridbot.db")


class StateStore:
    """SQLite-backed persistent state for crash recovery and analytics."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._conn = None  # aiosqlite connection, set in initialize()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open DB connection and create tables if needed."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close DB connection."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Grid config persistence
    # ------------------------------------------------------------------

    async def save_grid_config(self, config: GridConfig) -> None:
        raise NotImplementedError

    async def load_grid_config(self, symbol: str) -> GridConfig | None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Position persistence
    # ------------------------------------------------------------------

    async def save_position(self, position: Position) -> None:
        raise NotImplementedError

    async def load_position(self, symbol: str) -> Position | None:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Order map persistence
    # ------------------------------------------------------------------

    async def save_open_orders(self, symbol: str, orders: list[OpenOrder]) -> None:
        raise NotImplementedError

    async def load_open_orders(self, symbol: str) -> list[OpenOrder]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Fills ledger
    # ------------------------------------------------------------------

    async def record_fill(self, fill: Fill) -> None:
        raise NotImplementedError

    async def get_fills(self, symbol: str, since_ms: int | None = None) -> list[Fill]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Regime persistence
    # ------------------------------------------------------------------

    async def save_regime(self, symbol: str, regime: Regime, timestamp_ms: int) -> None:
        raise NotImplementedError

    async def load_regime(self, symbol: str) -> tuple[Regime, int] | None:
        """Returns (regime, transition_timestamp_ms) or None."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Pending flips (section 7.6)
    # ------------------------------------------------------------------

    async def save_pending_flips(self, symbol: str, flips: list[PendingFlip]) -> None:
        raise NotImplementedError

    async def load_pending_flips(self, symbol: str) -> list[PendingFlip]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Bot state & heartbeat
    # ------------------------------------------------------------------

    async def save_bot_state(self, symbol: str, state: AssetState) -> None:
        """Persist full asset state snapshot (for restart recovery)."""
        raise NotImplementedError

    async def load_bot_state(self, symbol: str) -> AssetState | None:
        raise NotImplementedError

    async def update_heartbeat(self, symbol: str, timestamp_ms: int) -> None:
        raise NotImplementedError

    async def get_last_heartbeat(self, symbol: str) -> int | None:
        raise NotImplementedError
