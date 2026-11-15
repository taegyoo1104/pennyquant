"""MySQL 8의 원자적 장부와 실행 환경별 서버 잠금을 제공한다.

기존 pq_ 테이블과 기록 식별자는 유지한다. DECIMAL 투영 테이블에는 주문·체결·
포지션·잔고의 수치를 저장하며 JSON 원장은 원래의 정보와 소수 문자열을 보존한다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import RLock
from typing import Iterator

from ..models import utc
from .serialization import dumps, exact_jsonable, record_identity
from .config import MySQLConfig, namespace_for

PRUNABLE_KINDS = ("price_samples", "quote_snapshots", "market_bars", "system_logs")


class MySQLRepository:
    backend = "mysql"

    def __init__(self, path: str | Path, config: MySQLConfig | None = None):
        # SQLite만 사용하는 경우 드라이버를 설치하지 않아도 되도록 늦게 가져온다.
        from .repository import KINDS
        self.kinds = KINDS
        self.path = Path(path)
        self.config = config or MySQLConfig.from_env()
        self.namespace = namespace_for(path, self.config.namespace, self.config.environment)
        self._mutex = RLock()
        self._in_transaction = False
        self._closed = False
        self._lease_depth = 0
        try:
            import pymysql
        except ImportError:
            raise RuntimeError("MySQL 의존성 PyMySQL[rsa]를 설치하세요") from None
        self._driver_errors = (pymysql.MySQLError,)
        try:
            self.db = pymysql.connect(host=self.config.host, port=self.config.port, user=self.config.user,
                password=self.config.password, database=self.config.database, charset="utf8mb4",
                autocommit=True, connect_timeout=self.config.connect_timeout, read_timeout=self.config.read_timeout,
                write_timeout=self.config.write_timeout, binary_prefix=True)
        except Exception:
            raise RuntimeError("MySQL 연결 실패: 로컬 서버·DB·사용자·환경변수를 확인하세요") from None
        try:
            row = self._execute("SELECT VERSION()", fetch="one")
            version = str(row[0]) if row else ""
            match = re.match(r"(\d+)\.", version)
            if "mariadb" in version.lower() or match is None or int(match.group(1)) < 8:
                raise RuntimeError("MySQL 8 이상이 필요합니다. 지원하는 MySQL 서버인지 확인하세요")
            self._execute("SET SESSION time_zone = '+00:00'")
            self._create_tables()
        except BaseException:
            self.close()
            raise

    def _execute(self, sql: str, params=(), *, fetch: str | None = None):
        with self._mutex:
            try:
                with self.db.cursor() as cursor:
                    cursor.execute(sql, params)
                    if fetch == "one":
                        return cursor.fetchone()
                    if fetch == "all":
                        return cursor.fetchall()
                    return cursor.rowcount
            except self._driver_errors:
                raise RuntimeError("MySQL 처리 실패: DB 연결과 SessionQuant 테이블 권한을 확인하세요") from None

    def _create_tables(self):
        for kind in self.kinds:
            self._execute(f"""CREATE TABLE IF NOT EXISTS pq_{kind} (
                seq BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
                record_id LONGTEXT NULL,
                record_hash BINARY(32) NULL,
                payload JSON NOT NULL,
                captured_at DATETIME(6) NOT NULL,
                created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
                UNIQUE KEY uq_namespace_record (namespace,record_hash),
                KEY ix_namespace_seq (namespace,seq),
                KEY ix_namespace_captured (namespace,captured_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""")
        self._execute("""CREATE TABLE IF NOT EXISTS pq_engine_state (
            namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
            payload JSON NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""")
        self._execute("""CREATE TABLE IF NOT EXISTS pq_controls (
            namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
            name VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
            payload JSON NOT NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            PRIMARY KEY(namespace,name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""")

        self._execute("""CREATE TABLE IF NOT EXISTS sq_financial_ledger (
            namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
            kind VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
            record_hash BINARY(32) NOT NULL, record_id LONGTEXT NOT NULL,
            symbol VARCHAR(20) COLLATE utf8mb4_bin NULL,
            quantity DECIMAL(65,30) NULL, price DECIMAL(65,30) NULL,
            amount DECIMAL(65,30) NULL, fee DECIMAL(65,30) NULL,
            cash DECIMAL(65,30) NULL, equity DECIMAL(65,30) NULL,
            reserved_cash DECIMAL(65,30) NULL, realized_pnl DECIMAL(65,30) NULL,
            stop_price DECIMAL(65,30) NULL, filled_quantity DECIMAL(65,30) NULL,
            filled_notional DECIMAL(65,30) NULL,
            updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
            PRIMARY KEY(namespace,kind,record_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""")
        self._execute("""CREATE TABLE IF NOT EXISTS sq_schema_versions (
            version INT NOT NULL PRIMARY KEY,
            description VARCHAR(255) NOT NULL,
            applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""")
        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(1,%s)",
                      ("기존 pq_ 원장과 실행 환경별 상태 저장",))
        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(2,%s)",
                      ("SessionQuant 시장·뉴스 원장과 정확한 DECIMAL 금융 수치 투영",))

        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(3,%s)",
                      ("시장 참조 ETF 관측·계산과 FRED 일별 수신 이력 분리",))

        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(4,%s)",
                      ("실제 수신·분석·전략 처리 시점의 불변 연구 기록",))

        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(5,%s)",
                      ("뉴스 수신·원문 검증 이력과 거시 맥락 스냅샷 추가",))
        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(6,%s)",
                      ("LLM 호출 감사·제한된 암호화 본문·이벤트 맥락 분석 추가",))
        self._execute("INSERT IGNORE INTO sq_schema_versions(version,description) VALUES(7,%s)",
                      ("감시 종목의 거래일별 1분봉·RVOL 비교 이력 저장",))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._mutex:
            if self._in_transaction:
                yield
                return
            self._execute("START TRANSACTION")
            self._in_transaction = True
            try:
                yield
                self.db.commit()
            except self._driver_errors:
                try:
                    self.db.rollback()
                except self._driver_errors:
                    pass
                raise RuntimeError("MySQL 커밋 결과를 확인할 수 없습니다. 실행을 중지하고 저장 상태를 대사하세요") from None
            except BaseException:
                try:
                    self.db.rollback()
                except self._driver_errors:
                    pass
                raise
            finally:
                self._in_transaction = False

    @contextmanager
    def engine_lease(self) -> Iterator[None]:
        """같은 MySQL DB·환경·네임스페이스의 동시 실행을 차단한다."""
        name = "pennyquant:" + sha256(f"{self.config.database}:{self.namespace}".encode()).hexdigest()[:48]
        if self._lease_depth:
            self._lease_depth += 1
            try:
                yield
            finally:
                self._lease_depth -= 1
            return
        row = self._execute("SELECT GET_LOCK(%s,0)", (name,), fetch="one")
        if not row or row[0] != 1:
            raise RuntimeError("같은 MySQL 환경에서 다른 엔진이 실행 중입니다")
        self._lease_depth = 1
        try:
            yield
        finally:
            self._lease_depth = 0
            if not self._closed:
                self._execute("SELECT RELEASE_LOCK(%s)", (name,), fetch="one")

    @staticmethod
    def _json(value) -> str:
        return dumps(value)

    @staticmethod
    def _decode(value):
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value

    def _kind(self, kind: str) -> str:
        if kind not in self.kinds:
            raise ValueError("지원하지 않는 기록 종류입니다")
        return "pq_" + kind

    def record(self, kind: str, value) -> None:
        table = self._kind(kind)
        data = exact_jsonable(value)
        if not isinstance(data, dict):
            raise ValueError("기록은 JSON 객체여야 합니다")
        from ..research.journal import KINDS as RECORDED, capture_record
        if kind in RECORDED or (kind == 'pipeline_status' and data.get('kind') == 'watchlist_focus'):
            with self.transaction():
                self.import_record(kind,data,record_identity(kind,data))
                capture_record(self,kind,data)
        else:
            self.import_record(kind,data,record_identity(kind,data))

    def import_record(self, kind: str, value: dict, record_id: str | None) -> None:
        """원래 기록 ID를 유지하며 JSON 원장과 금융 수치 투영을 함께 저장한다."""
        table = self._kind(kind)
        data = exact_jsonable(value)
        if not isinstance(data, dict):
            raise ValueError("기록은 JSON 객체여야 합니다")
        rid = record_id
        record_id = str(rid) if rid is not None else None
        record_hash = sha256(record_id.encode("utf-8")).digest() if record_id is not None else None
        payload = self._json(data)
        try:
            observed = utc(data.get("received_at") or data.get("timestamp") or data.get("analyzed_at"))
        except (ValueError, TypeError, AttributeError):
            observed = datetime.now(timezone.utc)
        captured_at = observed.replace(tzinfo=None)
        def write_record():
            self._execute(f"INSERT INTO {table}(namespace,record_id,record_hash,payload,captured_at) VALUES(%s,%s,%s,%s,%s) "
                          "ON DUPLICATE KEY UPDATE payload=%s,captured_at=%s,updated_at=CURRENT_TIMESTAMP(6)",
                          (self.namespace, record_id, record_hash, payload, captured_at, payload, captured_at))
        if kind in {"orders", "executions", "positions", "account_snapshots"} and record_hash is not None:
            with self.transaction():
                write_record()
                self._project_finance(kind, record_id, data)
        else:
            write_record()

    @staticmethod
    def _decimal(value):
        if value is None:
            return None
        try:
            if isinstance(value, bool):
                raise ValueError
            result = Decimal(str(value))
            if not result.is_finite() or result.adjusted() >= 35:
                raise ValueError
            digits = list(result.as_tuple().digits)
            exponent = result.as_tuple().exponent
            while digits and digits[-1] == 0:
                digits.pop()
                exponent += 1
            if digits and exponent < -30:
                raise ValueError
            return result
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("금융 수치는 DECIMAL(65,30)에 정확히 저장할 수 있는 유한 값이어야 합니다") from None

    def _project_finance(self, kind: str, record_id: str, data: dict):
        base = {**data.get("intent", {}), **data} if isinstance(data.get("intent"), dict) else data
        def pick(*names):
            return self._decimal(next((base[key] for key in names if base.get(key) is not None), None))
        values = (pick("qty", "quantity"), pick("price", "limit_price", "avg_price"),
                  pick("amount", "notional", "initial_amount"), pick("fee", "entry_fee"),
                  pick("cash"), pick("equity"), pick("reserved_cash"), pick("realized_pnl"),
                  pick("stop_price"), pick("filled_qty"), pick("filled_notional"))
        columns = "quantity,price,amount,fee,cash,equity,reserved_cash,realized_pnl,stop_price,filled_quantity,filled_notional"
        assignments = ",".join(f"{name}=%s" for name in columns.split(","))
        self._execute(f"INSERT INTO sq_financial_ledger(namespace,kind,record_hash,record_id,symbol,{columns}) "
                      f"VALUES({','.join(['%s'] * 16)}) ON DUPLICATE KEY UPDATE symbol=%s,{assignments}",
                      (self.namespace, kind, sha256(record_id.encode()).digest(), record_id, base.get("symbol"),
                       *values, base.get("symbol"), *values))

    def get_record(self, kind: str, record_id: str) -> dict | None:
        return self.get_records(kind, [record_id]).get(record_id)

    def get_records(self, kind: str, record_ids) -> dict[str, dict]:
        table = self._kind(kind)
        ids = list(dict.fromkeys(record_ids))
        if any(not isinstance(item, str) for item in ids):
            raise ValueError("기록 식별자는 문자열이어야 합니다")
        found = {}
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            placeholders = ",".join("%s" for _ in batch)
            hashes = [sha256(item.encode()).digest() for item in batch]
            rows = self._execute(f"SELECT record_id,payload FROM {table} WHERE namespace=%s AND record_hash IN ({placeholders})",
                                 (self.namespace, *hashes), fetch="all")
            found.update({row[0]: self._decode(row[1]) for row in rows})
        return found

    def namespace_has_data(self) -> bool:
        for name in (*("pq_" + kind for kind in self.kinds), "pq_engine_state", "pq_controls", "sq_financial_ledger"):
            if self._execute(f"SELECT 1 FROM {name} WHERE namespace=%s LIMIT 1", (self.namespace,), fetch="one"):
                return True
        return False

    def record_fingerprints(self, kind, record_ids, *, include_payload=False):
        """Hash JSON on the server so unchanged history does not cross the network."""
        table = self._kind(kind)
        ids = list(dict.fromkeys(record_ids))
        if any(not isinstance(item,str) for item in ids):
            raise ValueError('기록 식별자는 문자열이어야 합니다')
        found = {}
        for start in range(0,len(ids),200):
            batch = ids[start:start+200]
            hashes = [sha256(item.encode()).digest() for item in batch]
            placeholders = ','.join('%s' for _ in batch)
            extra = ',payload' if include_payload else ''
            rows = self._execute(f'SELECT record_id,SHA2(CAST(payload AS CHAR CHARACTER SET utf8mb4),256){extra} '
                                 f'FROM {table} WHERE namespace=%s AND record_hash IN ({placeholders})',
                                 (self.namespace,*hashes),fetch='all')
            for row in rows:
                found[row[0]] = {'fingerprint':row[1],**({'payload':self._decode(row[2])} if include_payload else {})}
        return found

    def iter_records(self,kind,*,batch_size=500):
        """다른 네임스페이스를 읽지 않는 순번 기반 페이지 조회."""
        table = self._kind(kind)
        if type(batch_size) is not int or not 1 <= batch_size <= 2000:raise ValueError('페이지 크기를 확인하세요.')
        after = 0
        while True:
            rows = self._execute(f'SELECT seq,payload FROM {table} WHERE namespace=%s AND seq>%s ORDER BY seq LIMIT %s',
                                 (self.namespace,after,batch_size),fetch='all')
            if not rows:break
            for seq,payload in rows:
                after = seq
                yield seq,json.loads(payload) if isinstance(payload,str) else payload

    def all(self, kind: str) -> list[dict]:
        rows = self._execute(f"SELECT payload FROM {self._kind(kind)} WHERE namespace=%s ORDER BY seq",
                             (self.namespace,), fetch="all")
        return [self._decode(row[0]) for row in rows]

    def recent(self, kind: str, limit: int = 2000) -> list[dict]:
        table = self._kind(kind)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit은 1~10000 정수여야 합니다")
        rows = self._execute(f"SELECT payload FROM {table} WHERE namespace=%s ORDER BY seq DESC LIMIT %s",
                             (self.namespace, limit), fetch="all")
        return [self._decode(row[0]) for row in reversed(rows)]

    def replace_positions(self, positions: dict) -> None:
        with self.transaction():
            self._execute("DELETE FROM pq_positions WHERE namespace=%s", (self.namespace,))
            self._execute("DELETE FROM sq_financial_ledger WHERE namespace=%s AND kind=%s", (self.namespace, "positions"))
            for value in positions.values():
                self.record("positions", value)

    def recent_llm_attempts(self, limit=500):
        """Keep namespace isolation and filter prechecks before the display limit."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('요청 조회 개수는 1~10000 정수여야 합니다.')
        rows = self._execute("SELECT payload FROM pq_llm_calls WHERE namespace=%s "
            "AND JSON_EXTRACT(payload,'$.network_attempted')=CAST('true' AS JSON) "
            "ORDER BY seq DESC LIMIT %s",(self.namespace,limit),fetch='all')
        return [self._decode(row[0]) for row in reversed(rows)]

    def save_state(self, state: dict) -> None:
        payload = self._json(state)
        account = state.get("account", state.get("broker", state))
        with self.transaction():
            self._execute("INSERT INTO pq_engine_state(namespace,payload) VALUES(%s,%s) ON DUPLICATE KEY UPDATE payload=%s",
                          (self.namespace, payload, payload))
            if isinstance(account, dict) and "cash" in account:
                self._project_finance("account_state", "current", exact_jsonable(account))

    def load_state(self) -> dict | None:
        row = self._execute("SELECT payload FROM pq_engine_state WHERE namespace=%s", (self.namespace,), fetch="one")
        return self._decode(row[0]) if row else None

    @staticmethod
    def _control_name(name: str):
        if not isinstance(name, str) or not 1 <= len(name) <= 191:
            raise ValueError("제어 값 이름은 1~191자여야 합니다")

    def set_control(self, name: str, value) -> None:
        self._control_name(name)
        payload = self._json(value)
        from ..research.journal import CONTROLS, capture_control
        with self.transaction():
            self._execute("INSERT INTO pq_controls(namespace,name,payload) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE payload=%s",
                          (self.namespace,name,payload,payload))
            if name in CONTROLS:capture_control(self,name,value)

    def get_control(self, name: str, default=None):
        self._control_name(name)
        row = self._execute("SELECT payload FROM pq_controls WHERE namespace=%s AND name=%s",
                            (self.namespace, name), fetch="one")
        return self._decode(row[0]) if row else default

    def update_control(self, name: str, update, default=None):
        """Lock the namespace/key before reading so independent connections cannot lose updates."""
        self._control_name(name)
        with self.transaction():
            self._execute("INSERT INTO pq_controls(namespace,name,payload) VALUES(%s,%s,%s) "
                          "ON DUPLICATE KEY UPDATE name=VALUES(name)",
                          (self.namespace, name, self._json(default)))
            row = self._execute("SELECT payload FROM pq_controls WHERE namespace=%s AND name=%s FOR UPDATE",
                                (self.namespace, name), fetch="one")
            value = update(self._decode(row[0]))
            self.set_control(name, value)
            return value

    def log(self, level: str, message: str, **extra) -> None:
        self.record("system_logs", {"timestamp": datetime.now(timezone.utc), "level": level, "message": message, **extra})

    def expire_llm_bodies(self, now, *, dry_run=False):
        """선택한 namespace의 만료된 감사 암호문만 정리한다."""
        stamp = utc(now).isoformat()
        with self.transaction():
            count = self._execute("SELECT COUNT(*) FROM pq_llm_audit_bodies WHERE namespace=%s "
                "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.expires_at'))<=%s", (self.namespace,stamp),fetch='one')[0]
            if not dry_run:
                self._execute("DELETE FROM pq_llm_audit_bodies WHERE namespace=%s "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.expires_at'))<=%s", (self.namespace,stamp))
            return count

    def storage_stats(self) -> dict:
        names = tuple("pq_" + kind for kind in self.kinds) + ("pq_engine_state", "pq_controls")
        physical_names = names + ("sq_financial_ledger", "sq_schema_versions")
        placeholders = ",".join(["%s"] * len(physical_names))
        rows = self._execute("SELECT TABLE_NAME,TABLE_ROWS,DATA_LENGTH,INDEX_LENGTH FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME",
            (self.config.database, *physical_names), fetch="all")
        tables = [{"table": row[0], "approx_rows": int(row[1] or 0), "data_bytes": int(row[2] or 0),
                   "index_bytes": int(row[3] or 0)} for row in rows]
        counts, sizes = {}, {}
        for name in names:
            row = self._execute(f"SELECT COUNT(*),COALESCE(SUM(JSON_STORAGE_SIZE(payload)),0) FROM {name} WHERE namespace=%s",
                                (self.namespace,), fetch="one")
            key = name.removeprefix("pq_")
            counts[key], sizes[key] = int(row[0]), int(row[1])
        return {"backend": self.backend, "database": self.config.database, "namespace": self.namespace,
                "namespace_rows": counts, "namespace_json_bytes": sizes,
                "namespace_payload_bytes": sum(sizes.values()), "tables": tables,
                "physical_table_bytes": sum(row["data_bytes"] + row["index_bytes"] for row in tables),
                "physical_scope": "all namespaces: SessionQuant 원장·금융 투영 테이블 전체. 서버 로그·백업은 별도"}

    def prune(self, before: datetime, kinds=PRUNABLE_KINDS, batch_size: int = 1000, *, dry_run: bool = True) -> dict[str, int]:
        before = utc(before).replace(tzinfo=None)
        if isinstance(kinds, str):
            raise ValueError("관찰 데이터(observational)인 price_samples, quote_snapshots, market_bars, system_logs만 정리할 수 있습니다")
        try:
            kinds = tuple(kinds)
        except TypeError:
            raise ValueError("정리 대상은 관찰 데이터 테이블 이름 목록이어야 합니다") from None
        if any(kind not in PRUNABLE_KINDS for kind in kinds):
            raise ValueError("관찰 데이터(observational)인 price_samples, quote_snapshots, market_bars, system_logs만 정리할 수 있습니다")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 10000:
            raise ValueError("정리 batch_size는 1~10000 정수여야 합니다")
        if type(dry_run) is not bool:
            raise ValueError("dry_run은 boolean이어야 합니다")
        if self._in_transaction:
            raise ValueError("엔진 트랜잭션 외부(outside)에서만 정리할 수 있습니다")
        result = {}
        for kind in dict.fromkeys(kinds):
            table = self._kind(kind)
            if dry_run:
                row = self._execute(f"SELECT COUNT(*) FROM {table} WHERE namespace=%s AND captured_at<%s",
                                     (self.namespace, before), fetch="one")
                result[kind] = int(row[0])
                continue
            count = 0
            while True:
                affected = self._execute(f"DELETE FROM {table} WHERE namespace=%s AND captured_at<%s ORDER BY seq LIMIT %s",
                                         (self.namespace, before, batch_size))
                count += affected
                if affected < batch_size:
                    break
            result[kind] = count
        return result

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.db.close()
            except self._driver_errors:
                pass
