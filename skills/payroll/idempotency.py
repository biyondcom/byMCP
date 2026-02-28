"""SQLite-based idempotency store for Qonto transfers."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from utils.logger import logger

DB_PATH = Path.home() / ".byMCP" / "idempotency.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transfers (
            idempotency_key TEXT PRIMARY KEY,
            employee_name   TEXT NOT NULL,
            period          TEXT NOT NULL,
            amount_cents    INTEGER NOT NULL,
            status          TEXT NOT NULL,
            transfer_id     TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def make_idempotency_key(name: str, period: str, amount_cents: int) -> str:
    raw = f"{name.strip().lower()}|{period}|{amount_cents}"
    return hashlib.sha256(raw.encode()).hexdigest()


def is_already_processed(key: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM transfers WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return row is not None and row["status"] == "success"


def record_pending(key: str, name: str, period: str, amount_cents: int) -> None:
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO transfers
                (idempotency_key, employee_name, period, amount_cents, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (key, name, period, amount_cents, now, now),
        )
        conn.commit()


def record_success(key: str, transfer_id: Optional[str] = None) -> None:
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE transfers SET status = 'success', transfer_id = ?, updated_at = ? WHERE idempotency_key = ?",
            (transfer_id, now, key),
        )
        conn.commit()


def record_failure(key: str, error: str) -> None:
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE transfers SET status = 'failed', updated_at = ? WHERE idempotency_key = ?",
            (now, key),
        )
        conn.commit()
    logger.debug("Transfer FAILED: %s: %s", key[:12], error)


def query_all(period: Optional[str] = None) -> List[dict]:
    with _connect() as conn:
        if period:
            rows = conn.execute(
                "SELECT * FROM transfers WHERE period = ? ORDER BY created_at DESC", (period,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transfers ORDER BY period DESC, created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]
