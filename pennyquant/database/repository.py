"""Small SQLite event journal and transactional paper-engine snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Iterator

from ..models import jsonable

KINDS = ("stocks", "market_bars", "quote_snapshots", "price_samples", "news_events", "signals", "orders",
         "executions", "positions", "daily_performance", "system_logs")


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        for name in KINDS:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {name} (seq INTEGER PRIMARY KEY, record_id TEXT UNIQUE, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS engine_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()
        self._in_transaction = False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._in_transaction = False

    def _commit(self) -> None:
        if not self._in_transaction:
            self.db.commit()

    def record(self, kind: str, value) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        data = jsonable(value)
        if not isinstance(data, dict):
            raise ValueError("record must be object")
        rid = data.get("id") or data.get("client_id")
        if kind == "positions":
            rid = data.get("symbol")
        elif kind == "news_events":
            rid = f"{data.get('id')}:{data.get('received_at')}"
        elif kind in ("market_bars", "quote_snapshots"):
            rid = f"{data.get('symbol')}:{data.get('timestamp')}:{data.get('received_at')}"
        self.db.execute(f"INSERT INTO {kind}(record_id,payload) VALUES (?,?) ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload",
                        (rid, json.dumps(data, ensure_ascii=False, allow_nan=False)))
        self._commit()

    def all(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {kind} ORDER BY seq")]

    def recent(self, kind: str, limit: int = 2000) -> list[dict]:
        """Bound live charts without deleting the stored observation history."""
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer between 1 and 10000")
        rows = list(self.db.execute(f"SELECT payload FROM {kind} ORDER BY seq DESC LIMIT ?", (limit,)))
        return [json.loads(row[0]) for row in reversed(rows)]

    def replace_positions(self, positions: dict) -> None:
        self.db.execute("DELETE FROM positions")
        for value in positions.values():
            self.record("positions", value)
        self._commit()

    def save_state(self, state: dict) -> None:
        self.db.execute("INSERT INTO engine_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (json.dumps(jsonable(state), ensure_ascii=False, allow_nan=False),))
        self._commit()

    def load_state(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM engine_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_control(self, name: str, value) -> None:
        self.db.execute("INSERT INTO controls VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                        (name, json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False)))
        self._commit()

    def get_control(self, name: str, default=None):
        row = self.db.execute("SELECT payload FROM controls WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def log(self, level: str, message: str, **extra) -> None:
        self.record("system_logs", {"timestamp": datetime.now(timezone.utc), "level": level, "message": message, **extra})

    def close(self):
        self.db.close()
