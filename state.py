"""
Lightweight SQLite persistence for trade records so that:
- the bot can recover open positions after a restart and still check their
  outcome once the binary contract expires
- you have a durable trade log/history for review
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional

import config
from models import TradeRecord

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    contract_type TEXT NOT NULL,
    deriv_contract_id TEXT NOT NULL,
    entry_spot REAL NOT NULL,
    entry_time TEXT NOT NULL,
    exit_spot REAL,
    exit_time TEXT,
    stake REAL NOT NULL,
    payout REAL,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    avg_confidence REAL NOT NULL,
    agreeing_models TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL,
    pnl_pct REAL
);
"""


def _migrate(conn: sqlite3.Connection):
    """Adds columns to an existing table that predates this field, if needed.
    Safe to run every startup — checks first, no-ops if already present."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "duration_minutes" not in existing_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60")
        logger.info("Migrated trades table: added duration_minutes column")


@contextmanager
def _conn():
    conn = sqlite3.connect(config.STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.execute(SCHEMA)
        _migrate(conn)
    logger.info(f"State DB ready at {config.STATE_DB_PATH}")


def save_trade(trade: TradeRecord) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (symbol, direction, contract_type, deriv_contract_id, entry_spot, entry_time,
                stake, payout, duration_minutes, avg_confidence, agreeing_models, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol, trade.direction, trade.contract_type, trade.deriv_contract_id,
                trade.entry_spot, trade.entry_time.isoformat(),
                trade.stake, trade.payout, trade.duration_minutes, trade.avg_confidence, trade.agreeing_models,
                trade.status,
            ),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_spot: Optional[float], exit_time: datetime,
                 pnl: float, pnl_pct: float, status: str):
    with _conn() as conn:
        conn.execute(
            """UPDATE trades SET
               exit_spot = ?, exit_time = ?, pnl = ?, pnl_pct = ?, status = ?
               WHERE id = ?""",
            (exit_spot, exit_time.isoformat(), pnl, pnl_pct, status, trade_id),
        )


def mark_failed(trade_id: int):
    with _conn() as conn:
        conn.execute("UPDATE trades SET status = 'failed' WHERE id = ?", (trade_id,))


def get_open_trades() -> List[TradeRecord]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
    return [_row_to_record(r) for r in rows]


def get_recent_trades(limit: int = 20) -> List[TradeRecord]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def _row_to_record(r: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=r["id"],
        symbol=r["symbol"],
        direction=r["direction"],
        contract_type=r["contract_type"],
        deriv_contract_id=r["deriv_contract_id"],
        entry_spot=r["entry_spot"],
        entry_time=datetime.fromisoformat(r["entry_time"]),
        exit_spot=r["exit_spot"],
        exit_time=datetime.fromisoformat(r["exit_time"]) if r["exit_time"] else None,
        stake=r["stake"],
        payout=r["payout"],
        duration_minutes=r["duration_minutes"],
        avg_confidence=r["avg_confidence"],
        agreeing_models=r["agreeing_models"],
        status=r["status"],
        pnl=r["pnl"],
        pnl_pct=r["pnl_pct"],
    )
