"""SQLite persistence for zones, event watches, events, alerts and runs.

Each row keeps its full JSON record plus a few indexed columns.  Events and
alerts are append-only logs keyed by deterministic IDs, so replaying the same
observation twice cannot create a duplicate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.fresh_zones.events import ZoneWatch
from app.fresh_zones.models import Freshness, Zone, ZoneStatus


_SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    zone_type TEXT NOT NULL,
    status TEXT NOT NULL,
    freshness TEXT NOT NULL,
    score INTEGER,
    param_version TEXT NOT NULL,
    parent_zone_id TEXT,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS zones_symbol ON zones(symbol, status);
CREATE TABLE IF NOT EXISTS watches (
    zone_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_zone ON events(zone_id, at);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    to_state TEXT NOT NULL,
    at TEXT NOT NULL,
    ist_date TEXT NOT NULL,
    delivered INTEGER NOT NULL,
    suppressed_reason TEXT,
    data TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_event ON alerts(event_id);
CREATE INDEX IF NOT EXISTS alerts_day ON alerts(ist_date, delivered);
CREATE INDEX IF NOT EXISTS alerts_zone ON alerts(zone_id, to_state, at);
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    param_version TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

_STATUS_RANK = {ZoneStatus.UNVERIFIED: 0, ZoneStatus.FRESH: 1, ZoneStatus.TOUCHED: 2, ZoneStatus.INVALIDATED: 3}


def default_db_path() -> str:
    return os.getenv(
        "FRESH_ZONE_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "fresh_zones.db")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def merge_zone(stored: Zone, recomputed: Zone) -> Zone:
    """Combine a stored zone with a fresh recomputation from history.

    History is authoritative for measurements, but a zone's lifecycle only
    moves forward: touches observed live are kept and a touched or
    invalidated zone is never made fresh again by a recomputation.
    """

    seen = {(t["date"], t["kind"]) for t in recomputed.touches}
    live_only = [t for t in stored.touches if (t["date"], t["kind"]) not in seen]
    if live_only:
        recomputed.touches = sorted([*recomputed.touches, *live_only], key=lambda t: t["date"])
        if len(stored.tests) > len(recomputed.tests):
            recomputed.tests = stored.tests
    if _STATUS_RANK[ZoneStatus(stored.status)] > _STATUS_RANK[ZoneStatus(recomputed.status)]:
        recomputed.status = stored.status
        recomputed.freshness = Freshness.TESTED if stored.status != ZoneStatus.UNVERIFIED else recomputed.freshness
        recomputed.freshness_reason = stored.freshness_reason or recomputed.freshness_reason
    if stored.invalidation and not recomputed.invalidation:
        recomputed.invalidation = stored.invalidation
    recomputed.flipped_zone_ids = sorted(set(stored.flipped_zone_ids) | set(recomputed.flipped_zone_ids))
    return recomputed


class FreshZoneStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory: sqlite3.Connection | None = None
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self.db_path == ":memory:":
                if self._memory is None:
                    self._memory = sqlite3.connect(":memory:", check_same_thread=False)
                    self._memory.row_factory = sqlite3.Row
                conn = self._memory
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                return
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ── Zones ───────────────────────────────────────────────────────────────
    def save_zones(self, zones: Iterable[Zone]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO zones (zone_id, symbol, timeframe, zone_type, status, freshness, score,
                                      param_version, parent_zone_id, updated_at, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(zone_id) DO UPDATE SET status=excluded.status, freshness=excluded.freshness,
                       score=excluded.score, updated_at=excluded.updated_at, data=excluded.data""",
                [
                    (
                        z.zone_id, z.symbol, z.timeframe, str(z.zone_type), str(z.status), str(z.freshness),
                        z.score, z.param_version, z.parent_zone_id, _now(), _dumps(z.to_dict()),
                    )
                    for z in zones
                ],
            )

    def get_zone(self, zone_id: str) -> Zone | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM zones WHERE zone_id = ?", (zone_id,)).fetchone()
        return Zone.from_dict(json.loads(row["data"])) if row else None

    def list_zones(
        self,
        *,
        symbol: str | None = None,
        statuses: Iterable[str] | None = None,
        param_version: str | None = None,
        limit: int | None = None,
    ) -> list[Zone]:
        clauses, args = [], []
        if symbol:
            clauses.append("symbol = ?")
            args.append(symbol)
        if statuses:
            statuses = list(statuses)
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if param_version:
            clauses.append("param_version = ?")
            args.append(param_version)
        sql = "SELECT data FROM zones"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY symbol, timeframe, zone_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [Zone.from_dict(json.loads(row["data"])) for row in rows]

    # ── Watches ─────────────────────────────────────────────────────────────
    def save_watch(self, symbol: str, watch: ZoneWatch) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO watches (zone_id, symbol, state, updated_at, data) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(zone_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at,
                       data=excluded.data""",
                (watch.zone_id, symbol, str(watch.state), _now(), _dumps(watch.to_dict())),
            )

    def get_watch(self, zone_id: str) -> ZoneWatch | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM watches WHERE zone_id = ?", (zone_id,)).fetchone()
        return ZoneWatch.from_dict(json.loads(row["data"])) if row else None

    def watched_symbols(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM watches ORDER BY symbol").fetchall()
        return [row["symbol"] for row in rows]

    def watches_for(self, symbol: str) -> list[ZoneWatch]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM watches WHERE symbol = ? ORDER BY zone_id", (symbol,)).fetchall()
        return [ZoneWatch.from_dict(json.loads(row["data"])) for row in rows]

    # ── Events ──────────────────────────────────────────────────────────────
    def save_events(self, events: Iterable[dict[str, Any]]) -> int:
        inserted = 0
        with self._connect() as conn:
            for event in events:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO events (event_id, zone_id, symbol, from_state, to_state, at, data)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event["event_id"], event["zone_id"], event["symbol"], event["from_state"],
                        event["to_state"], event["at"], _dumps(event),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def list_events(self, *, zone_id: str | None = None, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses, args = [], []
        if zone_id:
            clauses.append("zone_id = ?")
            args.append(zone_id)
        if symbol:
            clauses.append("symbol = ?")
            args.append(symbol)
        sql = "SELECT data FROM events" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, [*args, int(limit)]).fetchall()
        return [json.loads(row["data"]) for row in rows]

    # ── Alerts ──────────────────────────────────────────────────────────────
    def alert_exists(self, event_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM alerts WHERE event_id = ?", (event_id,)).fetchone() is not None

    def last_delivered(self, zone_id: str, to_state: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM alerts WHERE zone_id = ? AND to_state = ? AND delivered = 1 ORDER BY at DESC LIMIT 1",
                (zone_id, to_state),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def delivered_count(self, ist_date: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE ist_date = ? AND delivered = 1", (ist_date,)
            ).fetchone()
        return int(row["n"])

    def save_alert(self, alert: dict[str, Any]) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO alerts (alert_id, event_id, zone_id, symbol, to_state, at, ist_date,
                                                 delivered, suppressed_reason, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert["alert_id"], alert["event_id"], alert["zone_id"], alert["stock"]["symbol"],
                    alert["states"]["current"], alert["price"]["timestamp"], alert["ist_date"],
                    1 if alert["delivery"]["delivered"] else 0, alert["delivery"]["suppressed_reason"], _dumps(alert),
                ),
            )
        return cursor.rowcount == 1

    def list_alerts(self, *, ist_date: str | None = None, delivered: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, args = [], []
        if ist_date:
            clauses.append("ist_date = ?")
            args.append(ist_date)
        if delivered is not None:
            clauses.append("delivered = ?")
            args.append(1 if delivered else 0)
        sql = "SELECT data FROM alerts" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, [*args, int(limit)]).fetchall()
        return [json.loads(row["data"]) for row in rows]

    # ── Symbol metadata and runs ───────────────────────────────────────────
    def save_symbol(self, symbol: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO symbols (symbol, updated_at, data) VALUES (?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET updated_at=excluded.updated_at, data=excluded.data""",
                (symbol, _now(), _dumps(data)),
            )

    def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
        return json.loads(row["data"]) if row else None

    def record_run(self, kind: str, started_at: str, param_version: str, data: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (kind, started_at, finished_at, param_version, data) VALUES (?, ?, ?, ?, ?)",
                (kind, started_at, _now(), param_version, _dumps(data)),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, kind, started_at, finished_at, param_version, data FROM runs ORDER BY run_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [{**{k: row[k] for k in row.keys() if k != "data"}, "summary": json.loads(row["data"])} for row in rows]
