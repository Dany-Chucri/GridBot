#!/usr/bin/env python3
"""Post-soak analysis (Phase 8.4).

Reads a GridBot StateStore SQLite DB and prints a markdown report covering
the metrics listed in `docs/plans/implementation-plan.md` Phase 8.4:

  - Fill profitability after fees
  - Maker vs taker ratio (grid fills should be ~100% maker — takers imply
    flatten activity)
  - Per-symbol fill counts and traded notional
  - Grid config snapshot (step_bps) with a note on whether the fee floor
    was likely binding
  - Heartbeat liveness (gaps in the last-heartbeat vs. now)
  - Latest regime per symbol
  - Pending flips left over at the end of the window

Usage:
    python scripts/post_soak_analysis.py /opt/gridbot/data/gridbot.db
    python scripts/post_soak_analysis.py --since-hours 72 gridbot.db

Log-based metrics (regime transition counts, flatten attempts, reconcile
errors) are NOT derivable from the DB alone — tail `journalctl -u gridbot`
for those and cross-reference against this report.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Crude PnL reconstruction (average-cost, matches PnLMonitor's scheme).
# ---------------------------------------------------------------------------


@dataclass
class _PnLState:
    position: float = 0.0
    avg_entry: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    notional: float = 0.0
    fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0


def _apply_fill(s: _PnLState, price: float, size: float, side: str,
                fee: float, is_maker: int) -> None:
    signed = size if side == "buy" else -size
    s.fees += fee
    s.notional += abs(size) * price
    s.fills += 1
    if is_maker:
        s.maker_fills += 1
    else:
        s.taker_fills += 1

    old = s.position
    new = old + signed

    # Close-only path (opposite-sign or partial close)
    if old != 0 and (old * new) <= 0 and abs(new) < abs(old):
        # Full or partial close, no flip in sign
        closed = abs(signed)
        # Closing a long with a sell: pnl = (price - avg_entry) * size
        # Closing a short with a buy: pnl = (avg_entry - price) * size
        if old > 0:
            s.realized += (price - s.avg_entry) * closed
        else:
            s.realized += (s.avg_entry - price) * closed
        s.position = new
        if s.position == 0:
            s.avg_entry = 0.0
        return

    # Flip through zero: realize the closing portion, then re-open
    if old != 0 and (old * new) < 0:
        closed = abs(old)
        if old > 0:
            s.realized += (price - s.avg_entry) * closed
        else:
            s.realized += (s.avg_entry - price) * closed
        s.position = new
        s.avg_entry = price
        return

    # Increase position (same side or opening)
    if old == 0:
        s.position = new
        s.avg_entry = price
        return

    # Same-direction increase: update avg_entry
    abs_new = abs(new)
    s.avg_entry = (s.avg_entry * abs(old) + price * abs(signed)) / abs_new
    s.position = new


def _analyze_fills(rows: list[sqlite3.Row]) -> dict[str, _PnLState]:
    per_symbol: dict[str, _PnLState] = defaultdict(_PnLState)
    for r in rows:
        s = per_symbol[r["symbol"]]
        _apply_fill(
            s,
            price=r["price"],
            size=r["size"],
            side=r["side"],
            fee=r["fee"],
            is_maker=r["is_maker"],
        )
    return per_symbol


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ms / 1000))


def _section_fills(conn: sqlite3.Connection, since_ms: int | None) -> str:
    q = "SELECT * FROM fills"
    args: tuple = ()
    if since_ms is not None:
        q += " WHERE timestamp_ms >= ?"
        args = (since_ms,)
    q += " ORDER BY timestamp_ms ASC"
    rows = list(conn.execute(q, args))

    if not rows:
        return "## Fills\n\n_No fills in window._\n"

    per_sym = _analyze_fills(rows)
    out = ["## Fills\n"]
    out.append(
        "| Symbol | Fills | Maker% | Traded Notional (USD) | Fees (USD) | Realized PnL (USD) | Net (USD) |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for sym, s in sorted(per_sym.items()):
        maker_pct = 100.0 * s.maker_fills / s.fills if s.fills else 0.0
        net = s.realized - s.fees
        out.append(
            f"| {sym} | {s.fills} | {maker_pct:.1f}% | "
            f"{s.notional:,.2f} | {s.fees:,.4f} | {s.realized:,.4f} | {net:,.4f} |"
        )

    # Note about maker ratio
    notes: list[str] = []
    for sym, s in per_sym.items():
        if s.fills > 0 and s.taker_fills > 0:
            notes.append(
                f"- **{sym}**: {s.taker_fills} taker fills observed — expected only "
                "during emergency flatten. Cross-reference with `journalctl` for "
                "flatten events."
            )
    if notes:
        out.append("\n" + "\n".join(notes))

    return "\n".join(out) + "\n"


def _section_grid_config(conn: sqlite3.Connection) -> str:
    rows = list(conn.execute("SELECT * FROM grid_config"))
    if not rows:
        return "## Grid Config\n\n_No grid config persisted._\n"

    out = ["## Grid Config (latest snapshot)\n"]
    out.append("| Symbol | Anchor | range_atr | step_bps | Epoch | Updated |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {r['symbol']} | {r['anchor']:.4f} | {r['range_atr']:.2f} | "
            f"{r['step_bps']:.2f} | {r['epoch']} | {_fmt_ts(r['updated_ms'])} |"
        )

    # Fee-floor heuristic: design doc 5.4 says the floor is
    #   2 * maker_fee (0.2bps) + spread (~2bps) + slippage_buffer + safety (~1.5bps)
    # ~= 4-6 bps minimum. Larger step_bps means ATR dominated.
    out.append(
        "\n_Fee-floor heuristic_: step_bps near 4-6 suggests the friction floor was "
        "binding; larger values indicate ATR-based step was dominant. Tune "
        "`grid_step_bps_min`/`grid_step_bps_max` if the floor was binding persistently."
    )
    return "\n".join(out) + "\n"


def _section_regime_and_state(conn: sqlite3.Connection) -> str:
    out = ["## Bot State (latest snapshot)\n"]
    rows = list(conn.execute("SELECT symbol, state_json, updated_ms FROM bot_state"))
    if not rows:
        return "\n".join(out) + "_No bot_state rows._\n"

    out.append("| Symbol | bot_state | regime | position | equity | Last update |")
    out.append("|---|---|---|---|---|---|")
    for r in rows:
        d = json.loads(r["state_json"])
        pos = d.get("position")
        pos_size = pos["size"] if pos else 0.0
        out.append(
            f"| {r['symbol']} | {d.get('bot_state', '?')} | {d.get('regime', '?')} | "
            f"{pos_size:.6f} | {d.get('account_equity', 0.0):.2f} | "
            f"{_fmt_ts(r['updated_ms'])} |"
        )
    return "\n".join(out) + "\n"


def _section_heartbeat(conn: sqlite3.Connection, now_ms: int) -> str:
    rows = list(conn.execute("SELECT symbol, timestamp_ms FROM heartbeat"))
    if not rows:
        return "## Heartbeat\n\n_No heartbeat rows._\n"

    out = ["## Heartbeat\n"]
    out.append("| Symbol | Last heartbeat | Age (seconds) |")
    out.append("|---|---|---|")
    for r in rows:
        age_s = (now_ms - r["timestamp_ms"]) / 1000.0
        flag = " ⚠" if age_s > 60 else ""
        out.append(f"| {r['symbol']} | {_fmt_ts(r['timestamp_ms'])} | {age_s:,.1f}{flag} |")
    out.append(
        "\n_A heartbeat gap > 60s on a running process indicates a stalled cycle. "
        "Cross-reference with logs; expected age on a stopped bot is 'time since shutdown'._"
    )
    return "\n".join(out) + "\n"


def _section_pending_flips(conn: sqlite3.Connection) -> str:
    rows = list(conn.execute("SELECT * FROM pending_flips"))
    if not rows:
        return "## Pending Flips\n\n_None — clean flip queue._\n"

    out = ["## Pending Flips (leftovers at end of window)\n"]
    out.append("| Symbol | Price | Side | Size | Originating fill |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        out.append(
            f"| {r['symbol']} | {r['price']:.4f} | {r['side']} | {r['size']} | "
            f"{r['originating_fill_id']} |"
        )
    out.append(
        "\n_Persistent pending flips across many runs suggest flips are not being "
        "placed (ALO rejections, re-anchor stalls, or reconcile gaps). Investigate._"
    )
    return "\n".join(out) + "\n"


def build_report(db_path: Path, since_ms: int | None, now_ms: int) -> str:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        parts = [
            f"# GridBot Post-Soak Report\n",
            f"- **Database:** `{db_path}`",
            f"- **Generated:** {_fmt_ts(now_ms)}",
        ]
        if since_ms is not None:
            parts.append(f"- **Window start:** {_fmt_ts(since_ms)}")
        parts.append("")

        parts.append(_section_fills(conn, since_ms))
        parts.append(_section_grid_config(conn))
        parts.append(_section_regime_and_state(conn))
        parts.append(_section_heartbeat(conn, now_ms))
        parts.append(_section_pending_flips(conn))

        parts.append(
            "## Notes on log-based metrics\n\n"
            "These metrics are NOT derivable from the SQLite DB and require log "
            "inspection (`journalctl -u gridbot` or `/var/log/gridbot/gridbot.log`):\n\n"
            "- Regime transitions count (cross-reference with the latest regime above).\n"
            "- Number of flatten attempts and residual-position counts.\n"
            "- REST reconciliation divergence events.\n"
            "- ALO rejection rate (>5/min → warning per design 7.4).\n"
            "- Maintenance entry/exit count.\n"
        )
        return "\n".join(parts)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GridBot post-soak analysis report")
    p.add_argument("db", type=Path, help="Path to the StateStore SQLite DB")
    p.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="Only analyze fills from the last N hours (default: all)",
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Write the report to a file (default: stdout)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    now_ms = int(time.time() * 1000)
    since_ms = (
        now_ms - int(args.since_hours * 3600 * 1000)
        if args.since_hours is not None
        else None
    )
    report = build_report(args.db, since_ms, now_ms)
    if args.output:
        args.output.write_text(report)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
