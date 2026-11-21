"""Read-only, bounded probe of the next research-events retention page.

Run with the installed app's Python and environment. No schema initialization,
retention invocation, engine lease, provider calls, or persisted report writes.
"""
from contextlib import closing
from dataclasses import replace
import json
import os
from time import monotonic


def diagnose(repo, *, clock=monotonic, emit=None):
    from sessionquant.database.retention import CURSORS, REPORT, RetentionPolicy, _projection, retention_rules
    from sessionquant.models import utc

    started = clock()
    report = {"read_only": True, "namespace": repo.namespace, "queries": [], "status": "STARTED"}

    def read(label, sql, params=()):
        if clock() - started >= 60:
            raise TimeoutError("진단 전체 시간 한도")
        began = clock()
        entry = {"stage": label}
        try:
            rows = repo._execute(sql, params, fetch="all")
            entry["rows"] = len(rows)
            return rows
        except Exception as exc:
            entry.update(error=type(exc).__name__, code=getattr(exc, "code", None))
            raise
        finally:
            entry["seconds"] = round(clock() - began, 4)
            report["queries"].append(entry)
            if emit:
                emit(entry)

    try:
        # Session-local settings only. MySQL itself rejects persistent writes.
        repo._execute("SET SESSION TRANSACTION READ ONLY")
        repo._execute("SET SESSION max_execution_time=3000")
        repo._execute("SET SESSION lock_wait_timeout=3")
        controls = read("saved_failure", "SELECT name,payload FROM pq_controls WHERE namespace=%s AND name IN (%s,%s)",
                        (repo.namespace, REPORT, CURSORS))
        state = {name: json.loads(value) if isinstance(value, str) else value for name, value in controls}
        previous, cursors = state.get(REPORT, {}), state.get(CURSORS, {})
        if previous.get("namespace") != repo.namespace or not any(
                e.get("table") == "pq_research_events" and e.get("code") == 3024 for e in previous.get("errors", [])):
            raise ValueError("이 장부의 재현 기록 조회 시간 초과 기록이 없습니다.")
        policy = RetentionPolicy(**previous["policy"])
        identity = {"version": 1, "diagnostics_days": policy.diagnostics_days, "news_days": policy.news_days}
        if policy.intraday_auxiliary:
            identity.update(version=2, intraday_auxiliary=True)
        if cursors.get("policy") != identity:
            raise ValueError("실패 당시 정책과 현재 정리 위치가 다릅니다.")
        cutoff = utc(previous["tables"]["research_events"]["before"]).replace(tzinfo=None)
        position = cursors.get("positions", {}).get("research_events")
        report.update(saved_at=previous.get("finished_at"), before=str(cutoff), cursor=position)
        report["indexes"] = read("indexes", "SHOW INDEX FROM pq_research_events")
        report["table_estimate"] = read("table_estimate",
            "SELECT TABLE_ROWS,DATA_LENGTH,INDEX_LENGTH,DATA_FREE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='pq_research_events'")
        report["server_activity"] = read("server_activity", "SHOW GLOBAL STATUS WHERE Variable_name IN "
            "('Threads_running','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Innodb_buffer_pool_wait_free')")
        after, params = "", [repo.namespace, cutoff]
        if position:
            stamp = utc(position[0]).replace(tzinfo=None)
            after = " AND (captured_at>%s OR (captured_at=%s AND seq>%s))"
            params += [stamp, stamp, int(position[1])]
        # Exact maintenance page when <=200; larger policies are deliberately sampled.
        limit = min(policy.batch_size, 200)
        params.append(limit)
        query = ("SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,captured_at FROM pq_research_events "
                 "FORCE INDEX (ix_namespace_captured) WHERE namespace=%s AND captured_at<%s" + after +
                 " ORDER BY captured_at,seq LIMIT %s")
        report["key_plan"] = read("key_plan", "EXPLAIN " + query, tuple(params))
        keys = read("candidate_keys", query, tuple(params))
        report["candidate_count"] = len(keys)
        condition = next(c for k, _, c in retention_rules(policy) if k == "research_events")
        for offset in range(0, len(keys), 10):
            ids = [key[0] for key in keys[offset:offset+10]]
            values = (repo.namespace, *ids)
            where = " FROM pq_research_events FORCE INDEX (PRIMARY) WHERE namespace=%s AND seq IN (" + ",".join("%s" for _ in ids) + ")"
            query = "SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,captured_at,created_at,updated_at,(" + _projection("research_events") + "),(" + condition + ")" + where
            if offset == 0:
                report["payload_plan"] = read("payload_plan", "EXPLAIN " + query, values)
            try:
                # Discard contents; only timings/counts leave the process.
                read("payload_page_" + str(offset//10 + 1), query, values)
            except Exception:
                report["failed_ids"] = ids
                for label, columns in (("failed_page_keys_only", "seq,captured_at"),
                                       ("failed_page_sizes", "seq,JSON_STORAGE_SIZE(payload),payload->>'$.kind'")):
                    try:
                        report[label] = read(label, "SELECT /*+ MAX_EXECUTION_TIME(3000) */ " + columns + where, values)
                    except Exception:
                        pass
                raise
        report["status"] = "NOT_REPRODUCED_IN_PAGE" if keys else "NO_CANDIDATES"
    except Exception as exc:
        report.update(status="PROBE_STOPPED", error=type(exc).__name__, code=getattr(exc, "code", None))
        # No original SQL/exception text, credentials, news bodies or trade payloads.
    report["elapsed_seconds"] = round(clock() - started, 3)
    return report


def main():
    from sessionquant.database.config import MySQLConfig
    from sessionquant.database.mysql_repository import MySQLRepository
    from sessionquant.session_config import load_session_config
    config = load_session_config(os.environ.get("SQ_SESSION_CONFIG") or "config/sessionquant.realtime-paper.example.yaml")
    db = MySQLConfig.from_env()
    if config.database_backend != "mysql" or config.environment != "PAPER_LOCAL" or db.environment not in (None, "PAPER_LOCAL"):
        raise ValueError("MySQL 모의 장부에서만 진단합니다.")
    with closing(MySQLRepository(config.db_path, replace(db, environment="PAPER_LOCAL", connect_timeout=5,
                                                       read_timeout=5, write_timeout=5), initialize=False)) as repo:
        result = diagnose(repo, emit=lambda entry: print(json.dumps(entry, ensure_ascii=False), flush=True))
    print("RESULT " + json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": "START_FAILED", "error": type(exc).__name__, "code": getattr(exc, "code", None)}), flush=True)
        raise SystemExit(1) from None
