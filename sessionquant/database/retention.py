"""Bounded Paper maintenance of expired auxiliary data, after a verified backup.

Financial ledgers, RVOL history, cost accounting and recovery controls are never
age-pruned. Replay is retained by default; the opt-in intraday policy expires raw
replay inputs after backup and marks affected runs as incomplete for export.
"""
from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from time import monotonic
from zoneinfo import ZoneInfo

from ..models import utc

REPORT = "data_retention_v1"
CURSORS = "data_retention_cursors_v1"
REPLAY_PRUNED = "research_pruned_runs_v1"


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = False
    diagnostics_days: int = 7
    news_days: int = 30
    batch_size: int = 200
    max_rows: int = 50000
    max_seconds: int = 90
    intraday_auxiliary: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.intraday_auxiliary) is not bool:
            raise ValueError("자료 정리 사용 여부는 불리언이어야 합니다.")
        for name, low, high in (("diagnostics_days", 7, 365), ("news_days", 30, 3650),
                                ("batch_size", 1, 500), ("max_rows", 1, 100000), ("max_seconds", 1, 90)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"자료 정리 {name}: {low}~{high} 정수를 사용하세요.")
        if self.news_days < self.diagnostics_days:
            raise ValueError("뉴스 보관 기간은 진단 기록 보관 기간 이상이어야 합니다.")


# Explicit allowlist. No caller-supplied table names or SQL predicates.
DIAGNOSTIC_COPIES = ("news_ingestions", "news_enrichments", "macro_context_snapshots")
RULES = (
    *((kind, "diagnostics_days", "1=1") for kind in (
        *DIAGNOSTIC_COPIES, "market_regimes", "price_samples", "quote_snapshots", "system_logs")),
    ("macro_context_analysis", "diagnostics_days",
     "payload->>'$.status'='SKIPPED_PRECHECK' AND payload->'$.llm_requested'=CAST('false' AS JSON) "
     "AND JSON_LENGTH(payload->'$.llm_call_ids')=0"),
    ("pipeline_status", "diagnostics_days", "payload->>'$.kind' IN ('watchlist_focus','portfolio_selection')"),
    *((kind, "news_days", "1=1") for kind in ("news_events", "news_analysis", "macro_news", "macro_analysis")),
    ("research_events", "diagnostics_days",
     "payload->>'$.kind' IN ('news_ingestions','news_enrichments','macro_context_snapshots')"),
)
TIME_FIELDS = ("timestamp", "published_at", "received_at", "analyzed_at", "known_at",
               "available_at", "ingested_at", "valid_until", "evaluated_at", "context_at", "checked_at")


def retention_rules(policy):
    if not policy.intraday_auxiliary:
        return RULES
    from ..research.journal import KINDS, CONTROLS
    replay_kinds = sorted(KINDS | CONTROLS | {"recording_start", "recording_stop", "engine_batch", "pipeline_status", "market_history_sessions"})
    replay_condition = "payload->>'$.kind' IN (" + ",".join("'" + kind + "'" for kind in replay_kinds) + ")"
    daily = {"research_events", "pipeline_status", "price_samples", "quote_snapshots"}
    return tuple((kind, "intraday" if kind in daily else age,
                  replay_condition if kind == "research_events" else condition) for kind, age, condition in RULES)


def retention_cutoff(policy, age, now):
    if age == "intraday":
        from ..market.bars import session_bounds
        bounds = session_bounds(now)
        # Ended sessions can be archived at shutdown. Before/during the next
        # session protect today's rows; unknown calendars never clear today.
        if bounds is not None and now >= bounds[1]:
            return now
        return now.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    return now - timedelta(days=policy[age])


def _pruned_runs(repo):
    marker = repo.get_control(REPLAY_PRUNED, {})
    if not isinstance(marker, dict) or not isinstance(marker.get("runs", {}), dict):
        raise ValueError("재현 자료 정리 기록을 확인할 수 없습니다. 자료를 보존합니다.")
    return dict(marker.get("runs", {}))


def _mark_pruned_runs(repo, runs, report, cutoff):
    recorded = _pruned_runs(repo)
    changed = False
    for run in runs:
        # Preserve the first pre-deletion backup, not a later partial backup.
        if run not in recorded:
            recorded[run] = {"before": cutoff.isoformat(), "backup": report["backup"]}
            changed = True
    if len(recorded) > 10000:
        raise ValueError("재현 자료 정리 기록 한도에 도달했습니다. 자료를 보존합니다.")
    if changed:
        repo.set_control(REPLAY_PRUNED, {"version": 1, "runs": recorded})


def backup_name(receipt: str | Path | None, now: datetime) -> str:
    if receipt is None:
        raise ValueError("실제 정리는 이번 종료 때 생성한 백업 성공 기록이 필요합니다.")
    text = Path(receipt).read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"UPLOADED (\S+) (sessionquant-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9-]+\.tar\.gz)", text)
    if match is None or not timedelta(0) <= utc(now) - utc(match[1]) <= timedelta(hours=1):
        raise ValueError("최근 1시간 이내의 클라우드 백업 성공 기록이 아닙니다. 자료를 보존합니다.")
    return match[2]


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _protected_ids(repo, deadline, clock):
    """Read small projections, not whole ledgers. Incomplete discovery aborts deletion."""
    protected = {"news": set(), "selection": set()}
    for table, field, target in (("signals", "news_id", "news"), ("order_intents", "selection_id", "selection")):
        after = 0
        while True:
            if clock() >= deadline:
                raise TimeoutError("매매 근거 확인 시간 초과: 자료 정리를 시작하지 않았습니다.")
            rows = repo._execute(
                f"SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,payload->>'$.{field}' FROM pq_{table} FORCE INDEX (ix_namespace_seq) "
                "WHERE namespace=%s AND seq>%s ORDER BY seq LIMIT 500", (repo.namespace, after), fetch="all")
            for seq, value in rows:
                if value and value != "null":
                    protected[target].add(value)
                after = seq
            if len(rows) < 500:
                break
    return protected


def _projection(kind):
    # Full article bodies, quotes and engine batches never leave MySQL here.
    root = "$.data" if kind == "research_events" else "$"
    paths = {"id": root + ".id", "news_id": root + ".news_id", "article_id": root + ".article_id",
             "event_id": root + ".event.id"}
    paths.update({field: root + "." + field for field in TIME_FIELDS})
    paths["event_published_at"] = root + ".event.published_at"
    if kind == "research_events":
        paths["_record_kind"] = "$.kind"
        paths.update({"journal_" + field: "$." + field for field in ("received_at", "available_at", "ingested_at")})
        paths["journal_run_id"] = "$.run_id"
    return "JSON_OBJECT(" + ",".join(f"'{key}',JSON_EXTRACT(payload,'{path}')" for key, path in paths.items()) + ")"


@contextmanager
def _stage_timing(table, phase, *, batch=None):
    """Bounded local diagnostics; never retain SQL, JSON bodies or identities."""
    metrics = table.setdefault("timings", {}).setdefault(
        phase, {"calls": 0, "seconds": 0.0, "max_seconds": 0.0, "timeouts": 0})
    started = monotonic()  # Separate from the injectable policy/budget clock.
    try:
        yield
    except Exception as exc:
        if getattr(exc, "code", None) == 3024:
            metrics["timeouts"] += 1
            table["last_read_timeout"] = {"phase": phase, "code": 3024,
                **{key: value for key, value in (batch or {}).items()
                   if key in {"count", "first_seq", "last_seq", "after_seq", "query_limit_ms"}}}
        raise
    finally:
        elapsed = max(0.0, monotonic() - started)
        metrics["calls"] += 1
        metrics["seconds"] = round(metrics["seconds"] + elapsed, 6)
        metrics["max_seconds"] = round(max(metrics["max_seconds"], elapsed), 6)


def _read_payload_page(repo, kind, condition, keys, table, deadline, clock, *, cache_writes=False, use_metadata=True, cache_filter=None):
    """Split timed-out reads, never skip a record or authorize partial deletion."""
    if clock() >= deadline:
        raise TimeoutError("자료 조회 전체 시간 한도")
    table["read_phase"] = "PAYLOAD"
    table["read_batch"] = {"count": len(keys), "first_seq": keys[0][0], "last_seq": keys[-1][0]}
    from . import retention_metadata
    if use_metadata and retention_metadata.supports(repo, kind):
        table['read_phase'] = 'METADATA_READ'
        with _stage_timing(table, 'METADATA_READ', batch=table['read_batch']):
            cached = retention_metadata.read(repo, keys, condition)
        found = {row[0] for row in cached}
        table['metadata_hits'] = table.get('metadata_hits', 0) + len(cached)
        missing = [key for key in keys if key[0] not in found]
        if not missing:
            return cached
        rows = _read_payload_page(repo, kind, condition, missing, table, deadline, clock,
                                  cache_writes=cache_writes, use_metadata=False, cache_filter=cache_filter)
        return cached + rows
    placeholders = ",".join("%s" for _ in keys)

    def read(limit_ms):
        with _stage_timing(table, "PAYLOAD", batch={**table["read_batch"], "query_limit_ms": limit_ms}):
            return repo._execute(
                f"SELECT /*+ MAX_EXECUTION_TIME({limit_ms}) */ seq,captured_at,created_at,updated_at,"
                f"({_projection(kind)}),({condition}) FROM pq_{kind} FORCE INDEX (PRIMARY) "
                f"WHERE namespace=%s AND seq IN ({placeholders})",
                (repo.namespace, *(key[0] for key in keys)), fetch="all")

    try:
        rows = read(3000)
    except Exception as exc:
        from .mysql_repository import MySQLRepositoryError
        if not isinstance(exc, MySQLRepositoryError) or exc.code != 3024:
            raise
        if len(keys) == 1:
            # Splitting cannot make this legacy JSON any smaller. Give this
            # exact row one bounded recovery read, without skipping its safety
            # metadata or resetting the overall maintenance deadline.
            remaining_ms = int((deadline - clock()) * 1000)
            if remaining_ms <= 3000:
                raise TimeoutError("단일 자료 재조회에 필요한 전체 시간이 부족합니다.") from exc
            limit_ms = min(10000, remaining_ms)
            retry = {**table["read_batch"], "query_limit_ms": limit_ms, "recovered": False}
            table["single_read_retries"] = table.get("single_read_retries", 0) + 1
            table["last_single_read_retry"] = retry
            rows = read(limit_ms)
            retry["recovered"] = True
        else:
            table["read_splits"] = table.get("read_splits", 0) + 1
            half = len(keys) // 2
            # Reuse successful sub-batches even if a later batch exhausts budget.
            table["payload_batch_size"] = min(table.get("payload_batch_size", 10), half)
            return (_read_payload_page(repo, kind, condition, keys[:half], table, deadline, clock,
                                      cache_writes=cache_writes, use_metadata=use_metadata, cache_filter=cache_filter)
                    + _read_payload_page(repo, kind, condition, keys[half:], table, deadline, clock,
                                         cache_writes=cache_writes, use_metadata=use_metadata, cache_filter=cache_filter))
    cache_rows = []
    if cache_writes and retention_metadata.supports(repo, kind):
        cache_rows = [row for row in rows if cache_filter is None or cache_filter(row)]
    if cache_rows and deadline-clock() >= 3:
        table['read_phase'] = 'METADATA_WRITE'
        try:
            with _stage_timing(table, 'METADATA_WRITE', batch=table['read_batch']):
                retention_metadata.backfill(repo, cache_rows)
            table['metadata_backfill_checked'] = table.get('metadata_backfill_checked', 0) + len(cache_rows)
        except Exception as exc:
            from .mysql_repository import MySQLRepositoryError
            if not isinstance(exc, MySQLRepositoryError) or exc.code != 3024:
                raise
            # Only the optional cache transaction rolled back. Original rows
            # have already passed the bounded read; deletion still rechecks
            # the original predicate when no valid metadata is available.
            table['metadata_write_timeouts'] = table.get('metadata_write_timeouts', 0) + 1
        table['read_phase'] = 'PAYLOAD'
    return rows


def _unused(kind, metadata, cutoff, protected):
    if kind == "research_events" and protected.get("intraday_auxiliary"):
        run = metadata.get("journal_run_id")
        if not isinstance(run, str) or not run or len(run) > 128 or run == protected.get("active_run"):
            return False
    if any(metadata.get(key) in protected["news"] for key in ("id", "news_id", "article_id", "event_id")):
        return False
    if kind == "pipeline_status" and metadata.get("id") in protected["selection"]:
        return False
    for key, value in metadata.items():
        if (key.endswith("_at") or key in {"timestamp", "valid_until"}) and value is not None:
            try:
                if utc(value) >= cutoff:
                    return False
            except (ValueError, TypeError, AttributeError, OverflowError):
                return False  # Unknown dates cannot prove expiry.
    return True


def _generation_baseline(previous, kind, now):
    try:
        generation = previous["tables"][kind]["observation"]["generation"]
        sequence, measured = generation["high_water_seq"], utc(generation["measured_at"])
        if type(sequence) is int and sequence >= 0 and measured < now:
            return sequence, measured
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        pass
    return None


def _observe_retention(repo, report, previous, protected, removed_sequences, deadline, clock, now):
    """Use only spare cleanup budget; capped samples never imply an empty backlog."""
    remaining = min(2000, report["policy"]["max_rows"] - report["scanned"])
    summary = {"backlog_status": "UNKNOWN", "eligible_backlog_min": None,
               "oldest_eligible_age_days": None, "observation_scanned": 0}
    report["observability"] = summary

    class ObservationBudgetExhausted(TimeoutError):
        pass

    stage, query_limit_ms = "NOT_STARTED", None

    def read(phase, sql, params, limit):
        nonlocal remaining, stage, query_limit_ms
        stage = phase
        milliseconds = min(250, int((deadline - clock()) * 1000))
        query_limit_ms = max(0, milliseconds)
        if milliseconds < 1 or limit < 1 or remaining < limit:
            raise ObservationBudgetExhausted("TIME_LIMIT" if milliseconds < 1 else "ROW_LIMIT")
        timing = observation.setdefault("timings", {}).setdefault(
            phase, {"calls": 0, "seconds": 0.0, "max_seconds": 0.0, "timeouts": 0})
        started = monotonic()
        try:
            rows = repo._execute(f"SELECT /*+ MAX_EXECUTION_TIME({milliseconds}) */ {sql}", params,
                                 fetch="all", optional_observation=True)
        except Exception as exc:
            if getattr(exc, "code", None) == 3024:
                timing["timeouts"] += 1
            raise
        finally:
            seconds = monotonic() - started
            timing["calls"] += 1
            timing["seconds"] = round(timing["seconds"] + seconds, 6)
            timing["max_seconds"] = round(max(timing["max_seconds"], seconds), 6)
        remaining -= len(rows)
        summary["observation_scanned"] += len(rows)
        if clock() >= deadline:
            raise ObservationBudgetExhausted("TIME_LIMIT")
        return rows

    rules = retention_rules(RetentionPolicy(**report["policy"]))
    for index, (kind, age, condition) in enumerate(rules):
        observation = {"backlog_status": "UNKNOWN", "eligible_rows_min": None, "backlog_exact": False,
                       "oldest_eligible_age_days": None, "oldest_eligible_at": None,
                       "generation": {"rows_min": None, "exact": False, "interval_seconds": None,
                                      "measured_at": None, "high_water_seq": None}}
        report["tables"][kind]["observation"] = observation
        quota = min(128, remaining // (len(rules) - index))
        if quota < 3 or clock() >= deadline:
            observation.update(status="SKIPPED_BUDGET", budget_reason="ROW_LIMIT" if quota < 3 else "TIME_LIMIT")
            continue
        stage, query_limit_ms = "NOT_STARTED", None
        baseline = _generation_baseline(previous, kind, now)
        cutoff = retention_cutoff(report["policy"], age, now)
        try:
            # An index endpoint is a baseline, never an insertion count. Sequence
            # gaps and other namespaces must not be counted as generated rows.
            endpoint = read("HIGH_WATER", f"seq FROM pq_{kind} FORCE INDEX (ix_namespace_seq) "
                            "WHERE namespace=%s ORDER BY seq DESC LIMIT 1", (repo.namespace,), 1)
            high_water = max(endpoint[0][0] if endpoint else 0, removed_sequences[kind]["highest"],
                             baseline[0] if baseline else 0)
            generation = observation["generation"]
            generation.update(high_water_seq=high_water, measured_at=now.isoformat())
            backlog_limit = (quota - 1) // 2 if baseline else quota - 1
            rows = read("BACKLOG", f"seq,captured_at,created_at,updated_at,({_projection(kind)}),({condition}) "
                        f"FROM pq_{kind} FORCE INDEX (ix_namespace_captured) "
                        "WHERE namespace=%s AND captured_at<%s ORDER BY captured_at,seq LIMIT %s",
                        (repo.namespace, cutoff.replace(tzinfo=None), backlog_limit), backlog_limit)
            stage = "BACKLOG_VALIDATION"
            eligible = [captured for _, captured, created, updated, data, allowed in rows
                        if allowed and max(created, updated) < cutoff.replace(tzinfo=None)
                        and _unused(kind, _decode(data), cutoff, protected)]
            exact = len(rows) < backlog_limit
            observation.update(backlog_status="PRESENT" if eligible else "CLEAR" if exact else "UNKNOWN",
                               eligible_rows_min=len(eligible) if eligible or exact else None, backlog_exact=exact)
            if eligible:
                oldest = min(eligible).replace(tzinfo=timezone.utc)
                observation.update(oldest_eligible_at=oldest.isoformat(),
                                   oldest_eligible_age_days=round((now - oldest).total_seconds() / 86400, 3))
            if baseline:
                generation_limit = quota - 1 - backlog_limit
                # Read a covering index first. Evaluating JSON while walking a
                # secondary index needlessly loads many large replay payloads.
                if condition != "1=1":
                    generation_limit //= 2  # charge both keys and payload reads
                keys = read("GENERATION_KEYS", f"seq FROM pq_{kind} FORCE INDEX (ix_namespace_seq) "
                                "WHERE namespace=%s AND seq>%s AND seq<=%s ORDER BY seq LIMIT %s",
                                (repo.namespace, baseline[0], high_water, generation_limit), generation_limit)
                arrived = len(keys)
                payload_complete = True
                if condition != "1=1":
                    arrived, offset, batch_size = 0, 0, 10
                    while offset < len(keys):
                        batch = [row[0] for row in keys[offset:offset + batch_size]]
                        observation["read_batch"] = {"count": len(batch), "first_seq": batch[0], "last_seq": batch[-1]}
                        placeholders = ",".join(["%s"] * len(batch))
                        try:
                            arrivals = read("GENERATION_PAYLOAD",
                                f"seq,({condition}) FROM pq_{kind} FORCE INDEX (PRIMARY) "
                                f"WHERE namespace=%s AND seq IN ({placeholders}) ORDER BY seq",
                                (repo.namespace, *batch), len(batch))
                        except Exception as exc:
                            from .mysql_repository import MySQLRepositoryError
                            if not isinstance(exc, MySQLRepositoryError) or exc.code != 3024 or len(batch) == 1:
                                raise
                            observation["generation_read_splits"] = observation.get("generation_read_splits", 0) + 1
                            batch_size = max(1, len(batch) // 2)
                            continue
                        payload_complete = payload_complete and len(arrivals) == len(batch)
                        arrived += sum(bool(allowed) for _, allowed in arrivals)
                        offset += len(batch)
                generation.update(rows_min=arrived + removed_sequences[kind]["new_min"],
                                  exact=len(keys) < generation_limit and payload_complete and removed_sequences[kind]["exact"],
                                  interval_seconds=(now - baseline[1]).total_seconds())
            observation["status"] = "COMPLETE"
        except Exception as exc:
            # A failed read is unknown observability, not a reason to broaden
            # deletion or discard already committed maintenance results.
            exhausted = isinstance(exc, ObservationBudgetExhausted)
            observation["status"] = "BUDGET_EXHAUSTED" if exhausted else "FAILED"
            observation["error"] = "TimeoutError" if exhausted else type(exc).__name__
            code = getattr(exc, "code", None)
            observation["error_details"] = {"stage": stage, "code": code if type(code) is int else None,
                                            "query_limit_ms": query_limit_ms}
            if exhausted:
                observation["budget_reason"] = str(exc)
            observation["generation"] = {"rows_min": None, "exact": False, "interval_seconds": None,
                                         "measured_at": None, "high_water_seq": None}
    observations = [table["observation"] for table in report["tables"].values()]
    present = [row for row in observations if row["backlog_status"] == "PRESENT"]
    if present:
        summary.update(backlog_status="PRESENT", eligible_backlog_min=sum(row["eligible_rows_min"] for row in present),
                       oldest_eligible_age_days=max(row["oldest_eligible_age_days"] for row in present))
    elif all(row["backlog_status"] == "CLEAR" for row in observations):
        summary.update(backlog_status="CLEAR", eligible_backlog_min=0)
    limited = report["status"] == "PARTIAL"
    prior_observability = previous.get("observability", {})
    prior_limits = prior_observability.get("budget_limited_runs", 0) if isinstance(prior_observability, dict) else 0
    repeated = (prior_limits if type(prior_limits) is int and prior_limits >= 0 else 0) + 1 if limited else 0
    capacity_review = repeated >= 2 or any(
        row["observation"]["backlog_status"] == "PRESENT"
        and row["observation"]["generation"]["rows_min"] is not None
        and row["observation"]["generation"]["rows_min"] > row["deleted"] for row in report["tables"].values())
    summary.update(stop_reason="QUERY_TIMEOUT" if report.get("errors") else "PASS_COMPLETE" if not limited else "ROW_LIMIT"
                   if report["scanned"] >= report["policy"]["max_rows"] else "TIME_LIMIT",
                   budget_limited_runs=repeated, attention="CAPACITY_REVIEW" if capacity_review else
                   "BACKLOG" if present else summary["backlog_status"])
    report["inspected"] = report["scanned"] + summary["observation_scanned"]


def prune_expired(repo, policy: RetentionPolicy, *, apply=False, receipt=None, now=None, clock=monotonic):
    """Fair, resumable bounded passes; a preview never writes cursors or reports."""
    now = utc(now or datetime.now(timezone.utc))
    if type(apply) is not bool or repo.backend != "mysql" or repo.config.environment != "PAPER_LOCAL":
        raise ValueError("자료 정리는 MySQL PAPER_LOCAL에서만 지원합니다.")
    if repo._in_transaction:
        raise ValueError("엔진 트랜잭션 밖에서 정리하세요.")
    report = {"policy_version": 2 if policy.intraday_auxiliary else 1, "namespace": repo.namespace, "policy": asdict(policy), "dry_run": not apply, "started_at": now.isoformat(),
              "status": "DISABLED", "scanned": 0, "eligible": 0, "deleted": 0, "tables": {},
              "provider_calls": 0, "order_writes": 0, "llm_generation_calls": 0}
    if not policy.enabled:
        return report
    report["backup"] = backup_name(receipt, now) if apply else None
    rules = retention_rules(policy)
    deadline = clock() + policy.max_seconds
    with repo.engine_lease() if apply else nullcontext():
        protected = _protected_ids(repo, deadline, clock)
        if policy.intraday_auxiliary:
            state = repo.get_control("research_recording_v1", {}) or {}
            if not isinstance(state, dict) or type(state.get("active", False)) is not bool:
                raise ValueError("재현 기록 실행 상태를 확인할 수 없습니다.")
            active_run = state.get("run_id") if state.get("active") else None
            if state.get("active") and (not isinstance(active_run, str) or not active_run):
                raise ValueError("실행 중인 재현 기록을 확인할 수 없습니다.")
            protected.update(intraday_auxiliary=True, active_run=active_run)
            _pruned_runs(repo)
        previous = repo.get_control(REPORT, {})
        if not isinstance(previous, dict) or previous.get("namespace") != repo.namespace:
            previous = {}
        removed_sequences = {kind: {"highest": 0, "new_min": 0, "exact": True} for kind, _, _ in rules}
        saved = repo.get_control(CURSORS, {}) if apply else {}
        identity = {"version": 1, "diagnostics_days": policy.diagnostics_days, "news_days": policy.news_days}
        if policy.intraday_auxiliary:
            identity.update(version=2, intraday_auxiliary=True)
        cursors = saved.get("positions", {}) if isinstance(saved, dict) and saved.get("policy") == identity else {}
        positions = dict(cursors)
        pending = list(rules)
        next_kind = saved.get("next_kind") if saved and saved.get("policy") == identity else None
        if next_kind in {rule[0] for rule in RULES}:
            index = next(i for i, rule in enumerate(pending) if rule[0] == next_kind)
            pending = pending[index:] + pending[:index]
        for kind, age, _ in rules:
            report["tables"][kind] = {"before": retention_cutoff(report["policy"], age, now).isoformat(),
                "scanned": 0, "eligible": 0, "deleted": 0, "protected": 0, "complete": False}
        # Protect shutdown against unexpected lock contention. This connection is
        # dedicated to maintenance; the engine uses another connection/lease.
        previous_wait = repo._execute("SELECT @@SESSION.innodb_lock_wait_timeout", fetch="one")[0]
        if apply:
            repo._execute("SET SESSION innodb_lock_wait_timeout=2")
        try:
            while pending and report["scanned"] < policy.max_rows and clock() < deadline:
                kind, age, condition = pending.pop(0)
                table = report["tables"][kind]
                cutoff = retention_cutoff(report["policy"], age, now)
                naive = cutoff.replace(tzinfo=None)
                position = positions.get(kind)
                after_sql, params = "", [repo.namespace, naive]
                if position:
                    after_sql = " AND (captured_at>%s OR (captured_at=%s AND seq>%s))"
                    stamp = utc(position[0]).replace(tzinfo=None)
                    params += [stamp, stamp, int(position[1])]
                limit = min(policy.batch_size, table.get("key_batch_size", policy.batch_size),
                            policy.max_rows - report["scanned"])
                try:
                    # Read an index-only page before touching large JSON bodies.
                    # LIMIT on a projection/filesort can otherwise read the whole
                    # replay table even though the client receives only 200 rows.
                    table["read_phase"] = "KEYS"
                    table.pop("read_batch", None)
                    while True:
                        if clock() >= deadline:
                            raise TimeoutError("자료 조회 전체 시간 한도")
                        try:
                            with _stage_timing(table, "KEYS", batch={"count": limit,
                                               "after_seq": int(position[1]) if position else None}):
                                keys = repo._execute(
                                    "SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,captured_at "
                                    f"FROM pq_{kind} FORCE INDEX (ix_namespace_captured) "
                                    f"WHERE namespace=%s AND captured_at<%s{after_sql} ORDER BY captured_at,seq LIMIT %s",
                                    (*params, limit), fetch="all")
                            break
                        except Exception as exc:
                            from .mysql_repository import MySQLRepositoryError
                            if not isinstance(exc, MySQLRepositoryError) or exc.code != 3024 or limit == 1:
                                raise
                            table["read_splits"] = table.get("read_splits", 0) + 1
                            limit = max(1, limit // 2)
                            table["key_batch_size"] = limit
                    if clock() >= deadline:
                        pending.append((kind, age, condition))
                        break
                    rows = []
                    offset = 0
                    while offset < len(keys):
                        if clock() >= deadline:
                            break
                        batch = keys[offset:offset + table.get("payload_batch_size", 10)]
                        rows.extend(_read_payload_page(repo, kind, condition, batch, table, deadline, clock,
                                                       cache_writes=apply, cache_filter=lambda row: not (
                                                           row[5] and max(row[2], row[3]) < naive
                                                           and _unused(kind, _decode(row[4]), cutoff, protected))))
                        offset += len(batch)
                    rows.sort(key=lambda row: (row[1], row[0]))
                    if clock() < deadline and [(r[0], r[1]) for r in rows] != list(keys):
                        raise RuntimeError(f"자료 정리 조회 중 pq_{kind} 기록이 변경됐습니다. 재확인이 필요합니다.")
                except TimeoutError:
                    pending.append((kind, age, condition))
                    break
                except Exception as exc:
                    from .mysql_repository import MySQLRepositoryError
                    if not isinstance(exc, MySQLRepositoryError) or exc.code != 3024:
                        raise
                    # Keep the last committed cursor; a timeout never means an
                    # empty table or authorizes deletion of a partial page.
                    table["error"] = {"code": 3024, "phase": table["read_phase"], "message": "자료 조회 시간 초과"}
                    pending.insert(0, (kind, age, condition))
                    break
                # A slow SELECT must not authorize writes after the budget expired.
                if clock() >= deadline:
                    pending.append((kind, age, condition))
                    break
                selected_rows = [(seq, _decode(data)) for seq, _, created, updated, data, allowed in rows
                                 if allowed and max(created, updated) < naive and _unused(kind, _decode(data), cutoff, protected)]
                selected = [seq for seq, _ in selected_rows]
                if clock() >= deadline:
                    pending.append((kind, age, condition))
                    break
                removed = 0
                if apply and selected:
                    placeholders = ",".join("%s" for _ in selected)
                    # The replay tombstone and deletion commit together. A
                    # partially retained run must never pass as a full replay.
                    # Includes the deletion, replay marker and transaction commit.
                    with _stage_timing(table, "DELETE"), repo.transaction():
                        from . import retention_metadata
                        if retention_metadata.supports(repo, kind):
                            removed = retention_metadata.delete(repo, selected, naive, condition)
                        else:
                            removed = repo._execute(f"DELETE FROM pq_{kind} WHERE namespace=%s AND seq IN ({placeholders}) "
                                                f"AND captured_at<%s AND created_at<%s AND updated_at<%s AND ({condition})",
                                                (repo.namespace, *selected, naive, naive, naive))
                        if removed and kind == "research_events" and policy.intraday_auxiliary:
                            _mark_pruned_runs(repo, {data["journal_run_id"] for _, data in selected_rows}, report, cutoff)
                    removed_sequences[kind]["highest"] = max(removed_sequences[kind]["highest"], max(selected))
                    baseline = _generation_baseline(previous, kind, now)
                    if baseline:
                        old_selected = sum(seq <= baseline[0] for seq in selected)
                        removed_sequences[kind]["new_min"] += max(0, removed - old_selected)
                        removed_sequences[kind]["exact"] &= removed == len(selected)
                for key, count in (("scanned", len(rows)), ("eligible", len(selected)), ("deleted", removed)):
                    report[key] += count
                    table[key] += count
                table["protected"] += len(rows) - len(selected)
                if len(rows) < limit:
                    positions.pop(kind, None)
                    table["complete"] = True
                else:
                    positions[kind] = [rows[-1][1].replace(tzinfo=timezone.utc).isoformat(), rows[-1][0]]
                    pending.append((kind, age, condition))
                if apply:
                    # Delete first, then checkpoint: interruption may re-scan but
                    # cannot skip an undeleted batch. Every batch commits alone.
                    with _stage_timing(table, "CHECKPOINT"):
                        repo.set_control(CURSORS, {"policy": identity, "positions": positions,
                            "next_kind": pending[0][0] if pending else None})
            report["status"] = "PARTIAL" if pending else "COMPLETE"
            report["errors"] = [{"table": "pq_" + kind, **value["error"]}
                                for kind, value in report["tables"].items() if "error" in value]
            _observe_retention(repo, report, previous, protected, removed_sequences, deadline, clock, now)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            if apply:
                repo.set_control(REPORT, report)
            return report
        finally:
            if apply:
                repo._execute("SET SESSION innodb_lock_wait_timeout=%s", (previous_wait,))


def retention_command(config_path, *, apply=False, receipt=None):
    from ..session_config import load_session_config
    from .config import MySQLConfig
    from .mysql_repository import MySQLRepository
    config = load_session_config(config_path)
    db = MySQLConfig.from_env()
    if config.database_backend != "mysql" or db.environment not in {None, "PAPER_LOCAL"}:
        raise ValueError("자료 정리는 MySQL PAPER_LOCAL 설정에서 실행하세요.")
    policy = RetentionPolicy(**config.retention)
    with closing(MySQLRepository(config.db_path, replace(db, environment=config.environment))) as repo:
        return prune_expired(repo, policy, apply=apply, receipt=receipt)
