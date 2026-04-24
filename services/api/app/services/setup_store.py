from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


JSON_FIELDS = {"reasons", "risk_factors", "tags"}
BOOL_FIELDS = {"pinned", "ignored", "archived"}


class SetupStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracked_setups (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    company_name TEXT,
                    sector TEXT,
                    direction TEXT NOT NULL,
                    setup_label TEXT,
                    tracking_label TEXT,
                    scanner_bucket TEXT,
                    source_mode TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_evaluated_at TEXT,
                    expires_at TEXT,
                    timeframe_label TEXT,
                    timeframe_days INTEGER,
                    entry_price REAL,
                    current_price REAL,
                    target_price REAL,
                    extended_target_price REAL,
                    stop_loss REAL,
                    invalidation REAL,
                    confidence REAL,
                    model_confidence REAL,
                    evidence_confidence REAL,
                    evidence_status TEXT,
                    expected_move_pct REAL,
                    risk_level TEXT,
                    risk_reward REAL,
                    move_quality REAL,
                    relative_volume REAL,
                    intraday_volume_ratio REAL,
                    change_pct REAL,
                    reason_summary TEXT,
                    reasons TEXT,
                    risk_factors TEXT,
                    tags TEXT,
                    status TEXT NOT NULL,
                    result_pct REAL,
                    max_favorable_move REAL,
                    max_adverse_move REAL,
                    last_update_label TEXT,
                    last_update_note TEXT,
                    notes TEXT,
                    pinned INTEGER DEFAULT 0,
                    ignored INTEGER DEFAULT 0,
                    archived INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tracked_setups_symbol_status ON tracked_setups(symbol, status);
                CREATE INDEX IF NOT EXISTS idx_tracked_setups_detected_at ON tracked_setups(detected_at DESC);
                CREATE TABLE IF NOT EXISTS setup_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setup_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    label TEXT NOT NULL,
                    note TEXT,
                    meta TEXT,
                    FOREIGN KEY(setup_id) REFERENCES tracked_setups(id)
                );
                CREATE INDEX IF NOT EXISTS idx_setup_updates_setup_id_created_at
                    ON setup_updates(setup_id, created_at DESC);
                """
            )

    def _encode(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        for field in JSON_FIELDS:
            if field in payload:
                payload[field] = json.dumps(payload[field] or [])
        for field in BOOL_FIELDS:
            if field in payload:
                payload[field] = 1 if payload[field] else 0
        return payload

    def _decode_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for field in JSON_FIELDS:
            payload[field] = json.loads(payload.get(field) or "[]")
        for field in BOOL_FIELDS:
            payload[field] = bool(payload.get(field))
        return payload

    def insert_setup(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = self._encode(values)
        columns = ", ".join(payload.keys())
        placeholders = ", ".join(f":{key}" for key in payload)
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO tracked_setups ({columns}) VALUES ({placeholders})",
                payload,
            )
            row = connection.execute(
                "SELECT * FROM tracked_setups WHERE id = ?",
                (values["id"],),
            ).fetchone()
        return self._decode_row(row) or {}

    def update_setup(self, setup_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        if not values:
            return self.get_setup(setup_id)
        payload = self._encode(values)
        assignments = ", ".join(f"{column} = :{column}" for column in payload)
        payload["setup_id"] = setup_id
        with self._connect() as connection:
            connection.execute(
                f"UPDATE tracked_setups SET {assignments} WHERE id = :setup_id",
                payload,
            )
            row = connection.execute(
                "SELECT * FROM tracked_setups WHERE id = ?",
                (setup_id,),
            ).fetchone()
        return self._decode_row(row)

    def get_setup(self, setup_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tracked_setups WHERE id = ?",
                (setup_id,),
            ).fetchone()
        return self._decode_row(row)

    def get_open_setup(self, symbol: str, direction: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tracked_setups
                WHERE symbol = ?
                  AND direction = ?
                  AND archived = 0
                  AND ignored = 0
                  AND status IN ('active', 'watch_only')
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                (symbol.upper(), direction),
            ).fetchone()
        return self._decode_row(row)

    def list_setups(self, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [item for item in (self._decode_row(row) for row in rows) if item]

    def record_update(
        self,
        setup_id: str,
        created_at: str,
        label: str,
        note: str,
        meta: dict[str, Any] | None = None,
    ):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO setup_updates (setup_id, created_at, label, note, meta)
                VALUES (?, ?, ?, ?, ?)
                """,
                (setup_id, created_at, label, note, json.dumps(meta or {})),
            )

    def get_updates(self, setup_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM setup_updates
                WHERE setup_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (setup_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "setup_id": row["setup_id"],
                "created_at": row["created_at"],
                "label": row["label"],
                "note": row["note"],
                "meta": json.loads(row["meta"] or "{}"),
            }
            for row in rows
        ]

    def latest_updates(self, since_iso: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT u.*, s.symbol, s.direction, s.status, s.result_pct
                FROM setup_updates u
                JOIN tracked_setups s ON s.id = u.setup_id
                WHERE u.created_at >= ?
                ORDER BY u.created_at DESC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "setup_id": row["setup_id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "status": row["status"],
                "result_pct": row["result_pct"],
                "created_at": row["created_at"],
                "label": row["label"],
                "note": row["note"],
                "meta": json.loads(row["meta"] or "{}"),
            }
            for row in rows
        ]
