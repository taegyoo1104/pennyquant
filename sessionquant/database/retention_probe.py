"""Read-only, bounded proof that the current retention policy has no work.

Run while the application is stopped. A previous COMPLETE report is not proof:
protected rows remain, new records arrive and retention cutoffs advance. This
probe checks today's eligibility without a backup, cursor writes or deletion.
An incomplete/erroring read is UNKNOWN, never permission to skip maintenance.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from time import monotonic

from ..models import utc
from .retention import RetentionPolicy, _decode, _projection, _unused, retention_cutoff, retention_rules


class _ProbeLimit(Exception):
    pass


def probe_retention(repo, policy: RetentionPolicy, *, now=None, max_seconds=10,
                    max_rows=2048, clock=monotonic):
    """Only CLEAR proves no eligible rows; all uncertainty requires a follow-up.

    The row budget caps expired keys and financial-reference projections
    separately. Payload projections revisit at most max_rows keys, ten at a
    time. MySQL execution hints and the caller's process timeout bound reads;
    a stopped writer is required to use the answer as a startup decision.
    """
    if repo.backend != "mysql" or repo.config.environment != "PAPER_LOCAL" or repo._in_transaction:
        raise ValueError("자료 정리 확인은 트랜잭션 밖의 MySQL PAPER_LOCAL에서 실행하세요.")
    if (type(max_seconds) is not int or not 1 <= max_seconds <= 30
            or type(max_rows) is not int or not 1 <= max_rows <= 10000):
        raise ValueError("확인 한도는 1~30초, 1~10000행 정수를 사용하세요.")
    now = utc(now or datetime.now(timezone.utc))
    started = clock()
    deadline = started + max_seconds
    result = {"version": 1, "read_only": True, "namespace": repo.namespace,
              "checked_at": now.isoformat(), "policy": asdict(policy),
              "status": "DISABLED", "reason": "POLICY_DISABLED", "scanned": 0,
              "protected_scanned": 0, "queries": 0, "tables": {},
              "limits": {"seconds": max_seconds, "rows": max_rows}}
    stage, table_name = "NOT_STARTED", None

    def read(phase, sql, params):
        nonlocal stage
        stage = phase
        milliseconds = min(500, int((deadline - clock()) * 1000))
        if milliseconds < 1:
            raise _ProbeLimit("TIME_LIMIT")
        result["queries"] += 1
        rows = repo._execute(f"SELECT /*+ MAX_EXECUTION_TIME({milliseconds}) */ {sql}",
                             params, fetch="all")
        if clock() >= deadline:
            raise _ProbeLimit("TIME_LIMIT")
        return list(rows)

    def protections():
        # Unlike retention._protected_ids, this has a row cap and a smaller
        # per-query deadline. The reference fields and semantics are identical.
        protected = {"news": set(), "selection": set()}
        for kind, field, target in (("signals", "news_id", "news"),
                                     ("order_intents", "selection_id", "selection")):
            after = 0
            while True:
                limit = min(200, max_rows - result["protected_scanned"])
                if limit < 1:
                    raise _ProbeLimit("PROTECTION_ROW_LIMIT")
                rows = read("PROTECTIONS", f"seq,payload->>'$.{field}' FROM pq_{kind} "
                            "FORCE INDEX (ix_namespace_seq) WHERE namespace=%s AND seq>%s "
                            "ORDER BY seq LIMIT %s", (repo.namespace, after, limit))
                result["protected_scanned"] += len(rows)
                for seq, value in rows:
                    if value and value != "null":
                        protected[target].add(value)
                    after = seq
                if len(rows) < limit:
                    break
        if policy.intraday_auxiliary:
            # Fetch only the two required fields, not a whole replay/control
            # document, and preserve the deletion path's state validation.
            rows = read("REPLAY_STATE", "JSON_TYPE(payload),JSON_EXTRACT(payload,'$.active'),"
                        "JSON_EXTRACT(payload,'$.run_id') FROM pq_controls "
                        "WHERE namespace=%s AND name=%s LIMIT 1",
                        (repo.namespace, "research_recording_v1"))
            active, run = False, None
            if rows:
                shape, active, run = rows[0]
                active = False if active is None else _decode(active)
                run = None if run is None else _decode(run)
                if shape != "OBJECT" or type(active) is not bool:
                    raise ValueError("재현 기록 실행 상태를 확인할 수 없습니다.")
                if active and (not isinstance(run, str) or not run):
                    raise ValueError("실행 중인 재현 기록을 확인할 수 없습니다.")
            protected.update(intraday_auxiliary=True, active_run=run if active else None)
        return protected

    if policy.enabled:
        result.update(status="UNKNOWN", reason="NOT_FINISHED")
        protected = None
        try:
            for table_name, age, condition in retention_rules(policy):
                cutoff = retention_cutoff(result["policy"], age, now)
                naive = cutoff.replace(tzinfo=None)
                table = {"before": cutoff.isoformat(), "scanned": 0, "complete": False}
                result["tables"][table_name] = table
                position = None
                while True:
                    limit = min(200, max_rows - result["scanned"])
                    if limit < 1:
                        raise _ProbeLimit("ROW_LIMIT")
                    after_sql, params = "", [repo.namespace, naive]
                    if position is not None:
                        after_sql = " AND (captured_at>%s OR (captured_at=%s AND seq>%s))"
                        params.extend((position[1], position[1], position[0]))
                    keys = read("KEYS", f"seq,captured_at FROM pq_{table_name} "
                                "FORCE INDEX (ix_namespace_captured) WHERE namespace=%s "
                                f"AND captured_at<%s{after_sql} ORDER BY captured_at,seq LIMIT %s",
                                (*params, limit))
                    if keys and protected is None:
                        protected = protections()
                    for offset in range(0, len(keys), 10):
                        batch = keys[offset:offset + 10]
                        from . import retention_metadata
                        rows, missing = [], batch
                        if retention_metadata.supports(repo, table_name):
                            sql, params = retention_metadata.read_query(repo, batch, condition)
                            rows = read("METADATA_READ", sql, params)
                            found = {row[0] for row in rows}
                            table["metadata_hits"] = table.get("metadata_hits", 0) + len(rows)
                            missing = [key for key in batch if key[0] not in found]
                        if missing:
                            placeholders = ",".join("%s" for _ in missing)
                            rows += read("PAYLOAD", "seq,captured_at,created_at,updated_at,"
                                         f"({_projection(table_name)}),({condition}) FROM pq_{table_name} "
                                         f"FORCE INDEX (PRIMARY) WHERE namespace=%s AND seq IN ({placeholders})",
                                         (repo.namespace, *(key[0] for key in missing)))
                        rows.sort(key=lambda row: (row[1], row[0]))
                        if [(row[0], row[1]) for row in rows] != list(batch):
                            raise _ProbeLimit("ROWS_CHANGED")
                        result["scanned"] += len(rows)
                        table["scanned"] += len(rows)
                        if any(allowed and max(created, updated) < naive
                               and _unused(table_name, _decode(data), cutoff, protected)
                               for _, _, created, updated, data, allowed in rows):
                            result.update(status="NEEDS_CLEANUP", reason="ELIGIBLE_ROWS", table=table_name)
                            return result
                    if len(keys) < limit:
                        table["complete"] = True
                        break
                    position = keys[-1]
            result.update(status="CLEAR", reason="NO_ELIGIBLE_ROWS")
        except _ProbeLimit as exc:
            result.update(status="UNKNOWN", reason=str(exc), stage=stage, table=table_name)
        except Exception as exc:
            # No SQL, credential, article body or record identifier in output.
            from .mysql_repository import MySQLRepositoryError
            if not isinstance(exc, (MySQLRepositoryError, ValueError, TypeError, KeyError, AttributeError)):
                raise
            result.update(status="UNKNOWN", reason="READ_ERROR", stage=stage, table=table_name,
                          error={"type": type(exc).__name__, "code": getattr(exc, "code", None)})
        finally:
            result["elapsed_seconds"] = round(max(0.0, clock() - started), 3)
    else:
        result["elapsed_seconds"] = 0.0
    return result


def probe_with_engine_lease(repo, policy, **kwargs):
    """Exclude another engine while reading; release before backup or apply.

    GET_LOCK / RELEASE_LOCK are connection-scoped SELECTs, so this also works
    on the sealed read-only database without changing its write fingerprint.
    Only acquisition errors are classified here; unexpected probe failures
    still propagate and the context manager always releases the lease.
    """
    if not policy.enabled:
        return probe_retention(repo, policy, **kwargs)
    started = monotonic()
    with ExitStack() as stack:
        try:
            stack.enter_context(repo.engine_lease())
        except RuntimeError as exc:
            return {"version": 1, "read_only": True, "namespace": repo.namespace,
                    "checked_at": utc(kwargs.get("now") or datetime.now(timezone.utc)).isoformat(),
                    "policy": asdict(policy), "status": "UNKNOWN", "reason": "ENGINE_LEASE_UNAVAILABLE",
                    "stage": "ENGINE_LEASE", "scanned": 0, "protected_scanned": 0, "queries": 0,
                    "tables": {}, "elapsed_seconds": round(max(0.0, monotonic() - started), 3),
                    "error": {"type": type(exc).__name__, "code": getattr(exc, "code", None)}}
        return probe_retention(repo, policy, **kwargs)


def probe_command(config_path):
    from ..session_config import load_session_config
    from .config import MySQLConfig
    from .mysql_repository import MySQLRepository
    config = load_session_config(config_path)
    db = MySQLConfig.from_env()
    if config.database_backend != "mysql" or config.environment != "PAPER_LOCAL" or db.environment not in {None, "PAPER_LOCAL"}:
        raise ValueError("자료 정리 확인은 MySQL PAPER_LOCAL 설정에서 실행하세요.")
    policy = RetentionPolicy(**config.retention)
    # Even a sealed read-only database must remain untouched: no DDL/migration.
    with closing(MySQLRepository(config.db_path, replace(db, environment=config.environment,
                 connect_timeout=min(db.connect_timeout, 5), read_timeout=min(db.read_timeout, 5)),
                 initialize=False)) as repo:
        return probe_with_engine_lease(repo, policy)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="시작 전 남은 자료 정리 대상 확인 (읽기 전용)")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(probe_command(args.config), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
