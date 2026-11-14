"""이전 SQLite 자료와 신규 MySQL 저장 계층을 연결하는 공통 저장소."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Iterator

from .serialization import dumps, exact_jsonable, record_identity

KINDS = ("stocks", "market_bars", "quote_snapshots", "price_samples", "news_events", "signals", "orders",
         "executions", "positions", "daily_performance", "system_logs", "candidates", "news_analysis",
         "alerts", "pipeline_status", "order_intents", "broker_state",
         "market_regimes", "macro_news", "macro_analysis", "llm_usage", "session_state",
         "universe_history", "reconciliations", "account_snapshots", "schema_migrations",
         "macro_indicators", "economic_events")


class Repository:
    def __new__(cls, path: str | Path, *, backend: str | None = None):
        if cls is Repository:
            from .config import database_backend
            selected = backend if backend is not None else database_backend()
            if selected not in {"mysql", "sqlite"}:
                raise ValueError("저장소 backend는 mysql 또는 sqlite이어야 합니다")
            if selected == "mysql":
                from .mysql_repository import MySQLRepository
                return MySQLRepository(path)
        return super().__new__(cls)

    def __init__(self, path: str | Path, *, backend: str | None = None):
        self.backend = "sqlite"
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
    def engine_lease(self):
        """SQLite 실행자는 별도로 기존 파일 잠금을 유지한다."""
        yield self

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
        data = exact_jsonable(value)
        if not isinstance(data, dict):
            raise ValueError("record must be object")
        rid = record_identity(kind, data)
        self.import_record(kind, data, rid)

    def import_record(self, kind: str, value: dict, record_id: str | None) -> None:
        """기존 SQLite 기록의 원래 식별자를 그대로 이관한다."""
        if kind not in KINDS:
            raise ValueError("지원하지 않는 기록 종류입니다")
        data = exact_jsonable(value)
        rid = record_id
        self.db.execute(f"INSERT INTO {kind}(record_id,payload) VALUES (?,?) ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload",
                        (rid, dumps(data)))
        self._commit()

    def all(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {kind} ORDER BY seq")]

    def recent(self, kind: str, limit: int = 2000) -> list[dict]:
        """저장 이력은 보존하면서 화면에서 읽는 최근 기록 수를 제한한다."""
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer between 1 and 10000")
        rows = list(self.db.execute(f"SELECT payload FROM {kind} ORDER BY seq DESC LIMIT ?", (limit,)))
        return [json.loads(row[0]) for row in reversed(rows)]

    def get_record(self, kind: str, record_id: str) -> dict | None:
        return self.get_records(kind, [record_id]).get(record_id)

    def get_records(self, kind: str, record_ids) -> dict[str, dict]:
        if kind not in KINDS:
            raise ValueError("지원하지 않는 기록 종류입니다")
        ids = list(dict.fromkeys(record_ids))
        if any(not isinstance(item, str) for item in ids):
            raise ValueError("기록 식별자는 문자열이어야 합니다")
        found = {}
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db.execute(f"SELECT record_id,payload FROM {kind} WHERE record_id IN ({placeholders})", batch)
            found.update({row[0]: json.loads(row[1]) for row in rows})
        return found

    def namespace_has_data(self) -> bool:
        for kind in (*KINDS, "engine_state", "controls"):
            if self.db.execute(f"SELECT 1 FROM {kind} LIMIT 1").fetchone():
                return True
        return False

    def storage_stats(self) -> dict:
        counts, sizes = {}, {}
        for name in (*KINDS, "engine_state", "controls"):
            row = self.db.execute(f"SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM {name}").fetchone()
            counts[name], sizes[name] = int(row[0]), int(row[1])
        page_count = self.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return {"backend": self.backend, "database": str(self.path), "namespace": self.path.stem,
                "namespace_rows": counts, "namespace_json_bytes": sizes, "namespace_payload_bytes": sum(sizes.values()),
                "physical_table_bytes": page_count * page_size, "physical_scope": "SQLite 파일 내부 페이지; WAL 파일은 별도"}

    def replace_positions(self, positions: dict) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM positions")
            for value in positions.values():
                self.record("positions", value)

    def save_state(self, state: dict) -> None:
        self.db.execute("INSERT INTO engine_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                        (dumps(state),))
        self._commit()

    def load_state(self) -> dict | None:
        row = self.db.execute("SELECT payload FROM engine_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_control(self, name: str, value) -> None:
        self.db.execute("INSERT INTO controls VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                        (name, dumps(value)))
        self._commit()

    def get_control(self, name: str, default=None):
        row = self.db.execute("SELECT payload FROM controls WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def log(self, level: str, message: str, **extra) -> None:
        self.record("system_logs", {"timestamp": datetime.now(timezone.utc), "level": level, "message": message, **extra})

    def close(self):
        self.db.close()
