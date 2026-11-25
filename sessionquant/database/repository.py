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
         "macro_indicators", "economic_events", "market_reference_observations", "market_reference_metrics", "fred_reference_observations", "research_events", "news_ingestions", "news_enrichments", "macro_context_snapshots", "llm_calls", "llm_audit_bodies", "macro_context_analysis", "market_history_sessions")


class Repository:
    def __new__(cls, path: str | Path, *, backend: str | None = None, initialize: bool = True):
        if cls is Repository:
            from .config import database_backend
            selected = backend if backend is not None else database_backend()
            if selected not in {"mysql", "sqlite"}:
                raise ValueError("저장소 backend는 mysql 또는 sqlite이어야 합니다")
            if selected == "mysql":
                from .mysql_repository import MySQLRepository
                return MySQLRepository(path, initialize=initialize)
        return super().__new__(cls)

    def __init__(self, path: str | Path, *, backend: str | None = None, initialize: bool = True):
        self.backend = "sqlite"
        self.path = Path(path)
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
        else:
            # 화면은 기존 DB만 연다. 제어 요청 쓰기는 계속 허용한다.
            self.db = sqlite3.connect(self.path.resolve().as_uri()+"?mode=rw", uri=True,
                                      timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._in_transaction = False
        if not initialize:
            return
        for name in KINDS:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {name} (seq INTEGER PRIMARY KEY, record_id TEXT UNIQUE, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS engine_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 7:
            self.db.execute("PRAGMA user_version=7")
        self.db.commit()

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
        from ..research.journal import KINDS as RECORDED, capture_record
        if kind in RECORDED or (kind == 'pipeline_status' and data.get('kind') == 'watchlist_focus'):
            with self.transaction():
                self.import_record(kind, data, rid)
                capture_record(self,kind,data)
        else:
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

    def iter_records(self, kind, *, batch_size=500):
        """원본 순번을 유지하며 제한된 페이지로 큰 기록을 읽는다."""
        if kind not in KINDS or type(batch_size) is not int or not 1 <= batch_size <= 2000:
            raise ValueError('기록 종류·페이지 크기를 확인하세요.')
        after = 0
        while True:
            rows = self.db.execute(f'SELECT seq,payload FROM {kind} WHERE seq>? ORDER BY seq LIMIT ?', (after,batch_size)).fetchall()
            if not rows:break
            for seq,payload in rows:
                after = seq
                yield seq,json.loads(payload)

    def iter_credit_usage(self, checked_at, *, batch_size=500):
        """Project credit fields; retain ambiguous legacy times for Python checks.

        Only validated UTC rows entirely before the check are excluded. This
        avoids downloading old/wide payloads; JSON timestamp validation still
        scans the ledger in SQLite because legacy rows have no time index.
        """
        from ..models import utc
        if type(batch_size) is not int or not 1 <= batch_size <= 2000:
            raise ValueError('페이지 크기를 확인하세요.')
        cutoff=utc(checked_at).strftime('%Y-%m-%dT%H:%M:%S')
        fields=('timestamp','request_started_at','cost_usd','cost_status')
        # Multi-path extraction preserves JSON booleans; rebuilding json_object
        # from scalar extracts would silently turn an invalid boolean cost into 0/1.
        projection='json_extract(payload,'+','.join(f"'$.{name}'" for name in fields)+')'
        settled="json_extract(payload,'$.timestamp')"
        started=f"coalesce(nullif(json_extract(payload,'$.request_started_at'),''),{settled})"
        def canonical(value):
            return (f"(substr({value},11,1)='T' AND substr({value},1,4)<>'0000' "
                f"AND datetime(substr({value},1,19),'+0 days')=replace(substr({value},1,19),'T',' ') "
                f"AND substr({value},-6)='+00:00' AND (length({value})=25 OR "
                f"(length({value}) BETWEEN 27 AND 32 AND substr({value},20,1)='.' "
                f"AND substr({value},21,length({value})-26) NOT GLOB '*[^0-9]*')))")
        old=f"{canonical(settled)} AND {canonical(started)} AND substr({settled},1,19)<? AND substr({started},1,19)<?"
        after=0
        while True:
            rows=self.db.execute(f'SELECT seq,{projection} FROM llm_usage WHERE seq>? '
                f'AND NOT coalesce(({old}),0) ORDER BY seq LIMIT ?',
                (after,cutoff,cutoff,batch_size)).fetchall()
            if not rows:break
            for seq,payload in rows:
                after=seq
                yield dict(zip(fields,json.loads(payload)))
            if len(rows)<batch_size:break

    def all(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise ValueError(f"unknown record kind {kind}")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT payload FROM {kind} ORDER BY seq")]

    def recent_pipeline_summary(self, limit=100):
        from .pipeline_projection import expression, summary, validate_limit
        validate_limit(limit)
        rows = self.db.execute(f"SELECT {expression('sqlite')} FROM pipeline_status ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [summary(row[0]) for row in reversed(rows)]

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

    def recent_llm_attempts(self, limit=500):
        """Filter actual requests before limiting; prechecks must not hide requests."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('요청 조회 개수는 1~10000 정수여야 합니다.')
        rows = self.db.execute("SELECT payload FROM llm_calls WHERE "
            "json_type(payload,'$.network_attempted')='true' "
            "ORDER BY seq DESC LIMIT ?",(limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

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

    def record_fingerprints(self, kind, record_ids, *, include_payload=False):
        """Hash stored bytes in the DB; normal reads return no large JSON payloads."""
        from hashlib import sha256
        if kind not in KINDS:
            raise ValueError('지원하지 않는 기록 종류입니다')
        ids = list(dict.fromkeys(record_ids))
        if any(not isinstance(item,str) for item in ids):
            raise ValueError('기록 식별자는 문자열이어야 합니다')
        self.db.create_function('sq_payload_sha256',1,lambda value:sha256(value.encode('utf-8')).hexdigest(),deterministic=True)
        found = {}
        for start in range(0,len(ids),200):
            batch = ids[start:start+200]
            placeholders = ','.join('?' for _ in batch)
            extra = ',payload' if include_payload else ''
            rows = self.db.execute(f'SELECT record_id,sq_payload_sha256(payload){extra} FROM {kind} WHERE record_id IN ({placeholders})',batch)
            for row in rows:
                found[row[0]] = {'fingerprint':row[1],**({'payload':json.loads(row[2])} if include_payload else {})}
        return found

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

    def load_dashboard_state(self) -> dict | None:
        """Project current holdings/bases without transferring lifetime recovery history."""
        from .dashboard_projection import STATE, expression, decode
        row = self.db.execute(f"SELECT {expression(STATE, 'sqlite')} FROM engine_state WHERE id=1").fetchone()
        return decode(row[0], STATE) if row else None

    def load_dashboard_order(self, client_id: str, *, recovery_state=True) -> dict | None:
        """Recover one older fill's exact order reason, including legacy state-only orders."""
        if not isinstance(client_id, str) or not client_id or len(client_id) > 200:
            raise ValueError('주문 식별자를 확인하세요.')
        from .dashboard_projection import ORDER, expression, decode
        if not recovery_state:
            row = self.db.execute(f"SELECT {expression(ORDER, 'sqlite')} FROM orders WHERE record_id=?",
                                  (client_id,)).fetchone()
            return decode(row[0], ORDER) if row else None
        # json_each accepts arbitrary object keys (including quotes/backslashes)
        # and returns only the selected compact order to the dashboard process.
        row = self.db.execute(f"SELECT {expression(ORDER, 'sqlite')} FROM "
            "(SELECT value AS payload FROM engine_state,JSON_EACH(engine_state.payload,'$.broker.orders') "
            "WHERE engine_state.id=1 AND key=?)", (client_id,)).fetchone()
        return decode(row[0], ORDER) if row else None

    def set_control(self, name: str, value) -> None:
        from ..research.journal import CONTROLS, capture_control
        with self.transaction():
            self.db.execute("INSERT INTO controls VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                            (name, dumps(value)))
            if name in CONTROLS:capture_control(self,name,value)

    def get_control(self, name: str, default=None):
        row = self.db.execute("SELECT payload FROM controls WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def update_control(self, name: str, update, default=None):
        """Read and replace under a database write lock, including the first insert."""
        with self.transaction():
            value = update(self.get_control(name, default))
            self.set_control(name, value)
            return value

    def log(self, level: str, message: str, **extra) -> None:
        self.record("system_logs", {"timestamp": datetime.now(timezone.utc), "level": level, "message": message, **extra})

    def expire_llm_bodies(self, now, *, dry_run=False):
        """만료된 암호문만 정리하고 호출 메타데이터·금융 원장은 보존한다."""
        from ..models import utc
        stamp = utc(now).isoformat()
        with self.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM llm_audit_bodies WHERE json_extract(payload,'$.expires_at')<=?", (stamp,)).fetchone()[0]
            if not dry_run:
                self.db.execute("DELETE FROM llm_audit_bodies WHERE json_extract(payload,'$.expires_at')<=?", (stamp,))
            return count

    def close(self):
        self.db.close()
