"""DB 연결 검사, 보존적인 이관, 보관 기간과 논리 백업 도구."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from ..locking import exclusive_lock
from .mysql_repository import PRUNABLE_KINDS
from .repository import KINDS, Repository
from .serialization import dumps


def check_database(path: str | Path, *, backend: str | None = None) -> dict:
    """접속과 프로젝트 테이블 구성을 확인하고 비밀정보 없이 용량을 반환한다."""
    with closing(Repository(path, backend=backend)) as repo:
        result = repo.storage_stats()
        result["ready"] = True
        result["live_orders_enabled"] = False
        return result


def prune_database(path: str | Path, days: int = 7, kinds=PRUNABLE_KINDS, *, apply: bool = False) -> dict:
    """기본값은 예상 삭제 건수만 보여준다. 주문·체결·포지션은 삭제하지 않는다."""
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("보관 일수는 1일 이상 정수여야 합니다")
    if type(apply) is not bool:
        raise ValueError("apply 값은 boolean이어야 합니다")
    with closing(Repository(path)) as repo:
        if repo.backend != "mysql":
            raise ValueError("보관 기간 정리는 MySQL 저장소에서 지원합니다")
        before = datetime.now(timezone.utc) - timedelta(days=days)
        if apply:
            with repo.engine_lease():
                counts = repo.prune(before, kinds, dry_run=False)
        else:
            counts = repo.prune(before, kinds, dry_run=True)
        return {"dry_run": not apply, "before": before.isoformat(), "rows": counts}


def migrate_sqlite(source: str | Path, target_path: str | Path) -> dict:
    """SQLite 원본을 읽기 전용으로 열고 빈 MySQL 네임스페이스에 한 번 이관한다."""
    source = Path(source).resolve()
    if not source.is_file():
        raise ValueError("이관할 SQLite 원본 파일을 찾을 수 없습니다")
    with exclusive_lock(str(source) + ".engine.lock", message="SQLite 엔진을 종료한 뒤 이관하세요"):
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            original.execute("BEGIN")
            available = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"engine_state", "controls"}.issubset(available):
                raise ValueError("지원하는 PennyQuant SQLite 원장 형식이 아닙니다")
            with closing(Repository(target_path, backend="mysql")) as target:
                with target.engine_lease(), target.transaction():
                    if target.namespace_has_data():
                        raise ValueError("대상 MySQL 네임스페이스가 비어 있지 않습니다. 새 네임스페이스를 지정하세요")
                    counts = {}
                    for kind in KINDS:
                        if kind not in available:
                            continue
                        count = 0
                        for record_id, payload in original.execute(f"SELECT record_id,payload FROM {kind} ORDER BY seq"):
                            target.import_record(kind, json.loads(payload), record_id)
                            count += 1
                        counts[kind] = count
                    row = original.execute("SELECT payload FROM engine_state WHERE id=1").fetchone()
                    if row:
                        target.save_state(json.loads(row[0]))
                    for name, payload in original.execute("SELECT name,payload FROM controls"):
                        target.set_control(name, json.loads(payload))
                    target.record("schema_migrations", {"id": "sqlite_import_v1", "source": source.name,
                        "timestamp": datetime.now(timezone.utc), "rows": counts, "source_retained": True})
                return {"backend": "mysql", "namespace": target.namespace, "rows": counts, "source_retained": True}


def _controls(repo) -> list[tuple[str, object]]:
    if repo.backend == "mysql":
        rows = repo._execute("SELECT name,payload FROM pq_controls WHERE namespace=%s ORDER BY name",
                             (repo.namespace,), fetch="all")
    else:
        rows = repo.db.execute("SELECT name,payload FROM controls ORDER BY name")
    return [(row[0], json.loads(row[1]) if isinstance(row[1], (str, bytes)) else row[1]) for row in rows]


def _records(repo, kind: str) -> list[dict]:
    if repo.backend == "mysql":
        rows = repo._execute(f"SELECT record_id,payload FROM pq_{kind} WHERE namespace=%s ORDER BY seq",
                             (repo.namespace,), fetch="all")
    else:
        rows = repo.db.execute(f"SELECT record_id,payload FROM {kind} ORDER BY seq")
    return [{"record_id": row[0], "payload": json.loads(row[1]) if isinstance(row[1], (str, bytes)) else row[1]}
            for row in rows]


def backup_database(path: str | Path, output: str | Path, *, backend: str | None = None) -> dict:
    """소규모 로컬 원장을 일관된 JSON 백업으로 저장한다. 비밀번호는 포함하지 않는다."""
    output = Path(output)
    if output.exists():
        raise ValueError("백업 파일이 이미 있습니다. 새 출력 파일을 지정하세요")
    with closing(Repository(path, backend=backend)) as repo:
        with exclusive_lock(str(repo.path) + ".engine.lock", message="백업 전에 엔진을 종료하세요"):
            with repo.engine_lease(), repo.transaction():
                snapshot = {"format": "sessionquant_snapshot_v1", "created_at": datetime.now(timezone.utc),
                            "source_backend": repo.backend,
                            "records": {kind: _records(repo, kind) for kind in KINDS},
                            "state": repo.load_state(), "controls": _controls(repo)}
    output.parent.mkdir(parents=True, exist_ok=True)
    # x 모드로 경합 상황에서도 기존 백업을 덮어쓰지 않는다.
    with output.open("x", encoding="utf-8") as stream:
        stream.write(dumps(snapshot))
    return {"output": str(output), "bytes": output.stat().st_size, "live_orders_enabled": False}


def restore_database(source: str | Path, target_path: str | Path, *, backend: str | None = None) -> dict:
    """빈 저장소에 논리 백업을 원자적으로 복구한다. 실거래를 활성화하지 않는다."""
    snapshot = json.loads(Path(source).read_text(encoding="utf-8"))
    if snapshot.get("format") != "sessionquant_snapshot_v1" or not isinstance(snapshot.get("records"), dict):
        raise ValueError("지원하지 않는 백업 형식입니다")
    if set(snapshot["records"]) - set(KINDS):
        raise ValueError("현재 버전이 지원하지 않는 테이블이 백업에 있습니다")
    with closing(Repository(target_path, backend=backend)) as repo:
        with exclusive_lock(str(repo.path) + ".engine.lock", message="복구 전에 엔진을 종료하세요"):
            with repo.engine_lease(), repo.transaction():
                if repo.namespace_has_data():
                    raise ValueError("빈 저장소에만 복구할 수 있습니다")
                counts = {}
                for kind, records in snapshot["records"].items():
                    for record in records:
                        repo.import_record(kind, record["payload"], record["record_id"])
                    counts[kind] = len(records)
                if snapshot.get("state") is not None:
                    repo.save_state(snapshot["state"])
                for name, value in snapshot.get("controls", []):
                    repo.set_control(name, value)
                # 복구된 주문 이력은 보존하되 자동 실거래 재개를 허용하지 않는다.
                repo.set_control("restored_requires_review", True)
                repo.set_control("manual_halt", True)
                repo.set_control("live_activation", False)
        return {"backend": repo.backend, "rows": counts, "requires_review": True, "live_orders_enabled": False}
