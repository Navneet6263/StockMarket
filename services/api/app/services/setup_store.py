from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


JSON_FIELDS = {"reasons", "risk_factors", "tags", "rearm_evidence"}
BOOL_FIELDS = {"pinned", "ignored", "archived"}
SCHEMA_COLUMNS = {
    "suggested_at": "TEXT",
    "target_1": "REAL",
    "target_2": "REAL",
    "trailing_stop": "REAL",
    "scanner_call_status": "TEXT",
    "hold_or_exit": "TEXT",
    "reason_for_exit_decision": "TEXT",
    "current_pnl_pct": "REAL",
    "target_progress_pct": "REAL",
    "target_hit_at": "TEXT",
    "partial_book_at": "TEXT",
    "exit_suggested_at": "TEXT",
    "stop_loss_hit_at": "TEXT",
    "expired_at": "TEXT",
    "last_checked_at": "TEXT",
    "closed_at": "TEXT",
    "exit_reason": "TEXT",
    "lifecycle_state": "TEXT",
    "memory_sessions": "INTEGER DEFAULT 60",
    "memory_expires_at": "TEXT",
    "parent_setup_id": "TEXT",
    "setup_generation": "INTEGER DEFAULT 1",
    "rearmed_at": "TEXT",
    "rearmed_setup_id": "TEXT",
    "continuation_rearmed_at": "TEXT",
    "target_2_hit_at": "TEXT",
    "trailing_activated_at": "TEXT",
    "last_retest_at": "TEXT",
    "continuation_pivot": "REAL",
    "post_target_high": "REAL",
    "post_target_low": "REAL",
    "rearm_reason": "TEXT",
    "rearm_evidence": "TEXT",
    "rearm_blocked_at": "TEXT",
    "rearm_block_reason": "TEXT",
    "last_bar_date": "TEXT",
    "last_bar_low": "REAL",
    "last_bar_high": "REAL",
    "last_bar_close": "REAL",
}


class SetupStore:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
                    archived INTEGER DEFAULT 0,
                    lifecycle_state TEXT,
                    memory_sessions INTEGER DEFAULT 60,
                    memory_expires_at TEXT,
                    parent_setup_id TEXT,
                    setup_generation INTEGER DEFAULT 1,
                    rearmed_at TEXT,
                    rearmed_setup_id TEXT,
                    continuation_rearmed_at TEXT,
                    target_2_hit_at TEXT,
                    trailing_activated_at TEXT,
                    last_retest_at TEXT,
                    continuation_pivot REAL,
                    post_target_high REAL,
                    post_target_low REAL,
                    rearm_reason TEXT,
                    rearm_evidence TEXT,
                    rearm_blocked_at TEXT,
                    rearm_block_reason TEXT,
                    last_bar_date TEXT,
                    last_bar_low REAL,
                    last_bar_high REAL,
                    last_bar_close REAL
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
            existing = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(tracked_setups)").fetchall()
            }
            for column, definition in SCHEMA_COLUMNS.items():
                if column not in existing:
                    connection.execute(f"ALTER TABLE tracked_setups ADD COLUMN {column} {definition}")
            # Backfill lifecycle metadata without rewriting historical outcomes.
            # datetime(..., '+90 days') is a migration-safe approximation of the
            # default 60-session memory window for rows created by older builds.
            connection.execute(
                """
                UPDATE tracked_setups
                SET lifecycle_state = CASE
                    WHEN status = 'failed' THEN 'INVALIDATED'
                    WHEN status = 'expired' THEN 'EXPIRED'
                    WHEN status = 'passed' THEN 'TARGET_1_HIT'
                    WHEN status = 'watch_only' THEN 'WATCH'
                    ELSE 'ARMED'
                END
                WHERE lifecycle_state IS NULL OR lifecycle_state = ''
                """
            )
            connection.execute(
                "UPDATE tracked_setups SET memory_sessions = 60 WHERE memory_sessions IS NULL"
            )
            connection.execute(
                """
                UPDATE tracked_setups
                SET memory_expires_at = datetime(detected_at, '+90 days')
                WHERE memory_expires_at IS NULL OR memory_expires_at = ''
                """
            )
            connection.execute(
                "UPDATE tracked_setups SET setup_generation = 1 WHERE setup_generation IS NULL OR setup_generation < 1"
            )
            connection.execute(
                """
                UPDATE tracked_setups
                SET continuation_pivot = COALESCE(target_1, target_price)
                WHERE continuation_pivot IS NULL AND target_hit_at IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tracked_setups_memory
                ON tracked_setups(symbol, direction, memory_expires_at DESC)
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

    def insert_setup_if_no_open(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert a fresh lifecycle only when no open symbol/direction row exists."""
        payload = self._encode(values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM tracked_setups
                WHERE symbol = ?
                  AND direction = ?
                  AND archived = 0
                  AND ignored = 0
                  AND status IN ('active', 'watch_only')
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                (values["symbol"].upper(), values["direction"]),
            ).fetchone()
            if existing is not None:
                return self._decode_row(existing) or {}, False
            columns = ", ".join(payload.keys())
            placeholders = ", ".join(f":{key}" for key in payload)
            connection.execute(
                f"INSERT INTO tracked_setups ({columns}) VALUES ({placeholders})",
                payload,
            )
            created = connection.execute(
                "SELECT * FROM tracked_setups WHERE id = ?",
                (values["id"],),
            ).fetchone()
        return self._decode_row(created) or {}, True

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

    def get_rearm_candidate(
        self,
        symbol: str,
        direction: str,
        now_iso: str,
        *,
        include_manual: bool = False,
    ) -> dict[str, Any] | None:
        source_clause = "" if include_manual else "AND source_mode != 'manual'"
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM tracked_setups
                WHERE symbol = ?
                  AND direction = ?
                  AND archived = 0
                  AND ignored = 0
                  AND status IN ('passed', 'failed', 'expired')
                  AND memory_expires_at >= ?
                  AND (rearmed_setup_id IS NULL OR rearmed_setup_id = '')
                  {source_clause}
                ORDER BY COALESCE(last_evaluated_at, detected_at) DESC
                LIMIT 1
                """,
                (symbol.upper(), direction, now_iso),
            ).fetchone()
        return self._decode_row(row)

    def insert_rearmed_setup(
        self,
        values: dict[str, Any],
        parent_setup_id: str,
        rearmed_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create one fresh generation and link its resolved parent.

        The open-row check makes repeated live scans idempotent even when the
        same symbol remains in several scanner buckets.
        """
        payload = self._encode(values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM tracked_setups
                WHERE symbol = ?
                  AND direction = ?
                  AND archived = 0
                  AND ignored = 0
                  AND status IN ('active', 'watch_only')
                ORDER BY detected_at DESC
                LIMIT 1
                """,
                (values["symbol"].upper(), values["direction"]),
            ).fetchone()
            if existing is not None:
                return self._decode_row(existing) or {}, False

            parent = connection.execute(
                """
                SELECT * FROM tracked_setups
                WHERE id = ?
                  AND status IN ('passed', 'failed', 'expired')
                  AND (rearmed_setup_id IS NULL OR rearmed_setup_id = '')
                """,
                (parent_setup_id,),
            ).fetchone()
            if parent is None:
                linked = connection.execute(
                    "SELECT * FROM tracked_setups WHERE parent_setup_id = ? ORDER BY detected_at DESC LIMIT 1",
                    (parent_setup_id,),
                ).fetchone()
                return self._decode_row(linked) or {}, False

            columns = ", ".join(payload.keys())
            placeholders = ", ".join(f":{key}" for key in payload)
            connection.execute(
                f"INSERT INTO tracked_setups ({columns}) VALUES ({placeholders})",
                payload,
            )
            connection.execute(
                """
                UPDATE tracked_setups
                SET rearmed_at = ?, rearmed_setup_id = ?,
                    last_update_label = 'Setup re-armed',
                    last_update_note = 'A fresh confirmed setup created a new lifecycle generation.'
                WHERE id = ?
                """,
                (rearmed_at, values["id"], parent_setup_id),
            )
            created = connection.execute(
                "SELECT * FROM tracked_setups WHERE id = ?",
                (values["id"],),
            ).fetchone()
        return self._decode_row(created) or {}, True

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
