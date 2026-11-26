"""Index an existing JSON predicate without truncating legacy audit history.

Installation never backfills. An explicit, bounded maintenance pass must cover
every source header before enabling the indexed path for a namespace. Source
triggers maintain the index, including writes from old application versions.
Direct manual edits to this derived table require invalidation, a full rebuild
with backfill, and then a new coverage proof.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from time import monotonic

TABLE = 'sq_llm_attempt_index'
CONTROL = 'llm_attempt_index_coverage_v1'
TRIGGERS = ('sq_llm_attempt_update_v1', 'sq_llm_attempt_insert_v1')
OPTIONAL_ERRORS = {1142, 1143, 1146, 1227, 1419}


def predicate(source='payload'):
    # Deliberately preserve MySQL JSON comparison, not Python truthiness.
    return f"COALESCE(JSON_EXTRACT({source},'$.network_attempted')=CAST('true' AS JSON),0)"


def bodies():
    insert = (f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,network_attempted) '
              f'VALUES(NEW.seq,NEW.namespace,NEW.updated_at,1,{predicate("NEW.payload")})')
    return (f'BEGIN DELETE FROM {TABLE} WHERE source_seq=NEW.seq; {insert}; END', insert)


def _normalized(statement):
    return ' '.join(statement.split())


def trigger_signature(repo):
    """Check definition and installation identity, not just trigger names."""
    try:
        rows = repo._execute(
            'SELECT TRIGGER_NAME,EVENT_MANIPULATION,ACTION_TIMING,EVENT_OBJECT_TABLE,'
            'ACTION_STATEMENT,CREATED,SQL_MODE,CHARACTER_SET_CLIENT,COLLATION_CONNECTION,DATABASE_COLLATION '
            'FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s AND TRIGGER_NAME IN (%s,%s) '
            'ORDER BY TRIGGER_NAME', (repo.config.database, *TRIGGERS), fetch='all')
    except Exception as exc:
        if getattr(exc, 'code', None) in OPTIONAL_ERRORS:
            return None
        raise
    found = {row[0]: row for row in rows}
    for name, event, body in zip(TRIGGERS, ('UPDATE', 'INSERT'), bodies()):
        row = found.get(name)
        if (row is None or row[1:4] != (event, 'AFTER', 'pq_llm_calls') or row[5] is None
                or _normalized(row[4]) != _normalized(body)):
            return None
    return sha256(json.dumps(rows, default=str, ensure_ascii=True).encode()).hexdigest()


def install(repo):
    try:
        repo._execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            source_seq BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
            source_updated_at DATETIME(6) NOT NULL,
            version INT NOT NULL,
            network_attempted TINYINT NOT NULL,
            KEY ix_namespace_attempt_seq (namespace,network_attempted,source_seq),
            CONSTRAINT fk_llm_attempt_source FOREIGN KEY (source_seq)
                REFERENCES pq_llm_calls(seq) ON DELETE CASCADE ON UPDATE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
        existing = {row[0] for row in repo._execute(
            'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s '
            'AND TRIGGER_NAME IN (%s,%s)', (repo.config.database, *TRIGGERS), fetch='all')}
        for name, event, body in zip(TRIGGERS, ('UPDATE', 'INSERT'), bodies()):
            if name in existing:
                continue
            try:
                repo._execute(f'CREATE TRIGGER {name} AFTER {event} ON pq_llm_calls FOR EACH ROW {body}')
            except Exception as exc:
                if getattr(exc, 'code', None) != 1359:
                    raise
    except Exception as exc:
        if getattr(exc, 'code', None) not in OPTIONAL_ERRORS:
            raise
        logging.getLogger(__name__).warning('LLM 요청 색인 사용 불가: 기존 조회로 처리합니다.')


def _legacy(repo, limit):
    rows = repo._execute('SELECT payload FROM pq_llm_calls FORCE INDEX (ix_namespace_seq) WHERE namespace=%s '
        "AND JSON_EXTRACT(payload,'$.network_attempted')=CAST('true' AS JSON) "
        'ORDER BY seq DESC LIMIT %s', (repo.namespace, limit), fetch='all')
    return [repo._decode(row[0]) for row in reversed(rows)]


def covered(receipt, namespace, signature):
    return (bool(signature) and isinstance(receipt, dict) and receipt.get('complete') is True
            and type(receipt.get('version')) is int and receipt['version'] == 1
            and receipt.get('namespace') == namespace and receipt.get('trigger_signature') == signature)


def recent(repo, limit):
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('요청 조회 개수는 1~10000 정수여야 합니다.')
    signature = trigger_signature(repo)
    with repo.transaction():
        if not signature or not covered(repo.get_control(CONTROL), repo.namespace, signature):
            return _legacy(repo, limit)
        try:
            keys = repo._execute(
                f'SELECT source_seq,source_updated_at FROM {TABLE} FORCE INDEX (ix_namespace_attempt_seq) '
                'WHERE namespace=%s AND network_attempted=1 AND version=1 ORDER BY source_seq DESC LIMIT %s',
                (repo.namespace, limit), fetch='all')
            if not keys:
                return []
            placeholders = ','.join('%s' for _ in keys)
            rows = repo._execute('SELECT seq,updated_at,payload FROM pq_llm_calls FORCE INDEX (PRIMARY) '
                f'WHERE namespace=%s AND seq IN ({placeholders})',
                (repo.namespace, *(row[0] for row in keys)), fetch='all')
        except Exception as exc:
            if getattr(exc, 'code', None) not in OPTIONAL_ERRORS:
                raise
            return _legacy(repo, limit)
        found = {row[0]: row for row in rows}
        if any(seq not in found or found[seq][1] != stamp for seq, stamp in keys):
            return _legacy(repo, limit)
        return [repo._decode(found[seq][2]) for seq, _ in reversed(keys)]


def invalidate(repo):
    repo.set_control(CONTROL, {'version': 1, 'namespace': repo.namespace, 'complete': False})


def backfill_page(repo, *, after=0, limit=200):
    """Explicit maintenance only; at most one locked page, with no source edits.

    Call with after=0 to begin/restart a pass. The returned cursor may be used
    to continue; completing a backfill does not itself authorize fast reads.
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('색인 보충 범위를 확인하세요.')
    if not trigger_signature(repo):
        raise ValueError('LLM 요청 색인 트리거를 확인할 수 없습니다.')
    with repo.transaction():
        invalidate(repo)
        keys = repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq FROM pq_llm_calls FORCE INDEX (ix_namespace_seq) '
            'WHERE namespace=%s AND seq>%s ORDER BY seq LIMIT %s FOR UPDATE',
            (repo.namespace, after, limit), fetch='all')
        if not keys:
            return {'after': after, 'rows': 0, 'done': True}
        placeholders = ','.join('%s' for _ in keys)
        rows = repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,namespace,updated_at,'
            f'{predicate()} FROM pq_llm_calls FORCE INDEX (PRIMARY) '
            f'WHERE namespace=%s AND seq IN ({placeholders})',
            (repo.namespace, *(row[0] for row in keys)), fetch='all')
        values = ','.join('(%s,%s,%s,1,%s)' for _ in rows)
        repo._execute(f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,network_attempted) '
            f'VALUES {values} ON DUPLICATE KEY UPDATE namespace=VALUES(namespace),'
            'source_updated_at=VALUES(source_updated_at),version=1,network_attempted=VALUES(network_attempted)',
            tuple(value for row in rows for value in row))
        return {'after': keys[-1][0], 'rows': len(rows), 'done': len(keys) < limit}


def prove_coverage(repo, *, max_seconds=30, batch_size=500, clock=monotonic):
    """Explicit maintenance proof: scan bounded header pages, never source JSON.

    A time limit or any missing/stale row leaves the fast path disabled. Source
    INSERT/UPDATE triggers cover changes concurrent with this verification.
    """
    if type(max_seconds) is not int or not 1 <= max_seconds <= 300:
        raise ValueError('색인 검사 시간 한도를 확인하세요.')
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError('색인 검사 묶음 크기를 확인하세요.')
    invalidate(repo)
    signature = trigger_signature(repo)
    if not signature:
        return False
    deadline, after = clock() + max_seconds, 0
    while clock() < deadline:
        keys = repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq,updated_at FROM pq_llm_calls '
            'FORCE INDEX (ix_namespace_seq) WHERE namespace=%s AND seq>%s ORDER BY seq LIMIT %s',
            (repo.namespace, after, batch_size), fetch='all')
        if keys:
            placeholders = ','.join('%s' for _ in keys)
            # Compare current source headers in one snapshot so a concurrent
            # source update does not produce a false stale timestamp result.
            missing = repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ r.seq '
                'FROM pq_llm_calls r FORCE INDEX (PRIMARY) '
                f'LEFT JOIN {TABLE} m ON m.source_seq=r.seq AND m.namespace=r.namespace '
                'AND m.source_updated_at=r.updated_at AND m.version=1 '
                f'WHERE r.namespace=%s AND r.seq IN ({placeholders}) AND m.source_seq IS NULL LIMIT 1',
                (repo.namespace, *(row[0] for row in keys)), fetch='one')
            if missing:
                return False
            after = keys[-1][0]
        if len(keys) < batch_size:
            if clock() >= deadline or trigger_signature(repo) != signature:
                return False
            repo.set_control(CONTROL, {'version': 1, 'namespace': repo.namespace, 'complete': True,
                'trigger_signature': signature, 'checked_through': after,
                'checked_at': datetime.now(timezone.utc).isoformat()})
            return True
    return False
