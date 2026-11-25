"""Small, transactionally maintained metadata for research retention only.

No source JSON, financial records or deletion policy are changed. Legacy rows
without metadata take the original checked path until a bounded cleanup reads
them. Database triggers cover all writers, including older application versions.
"""
import logging

TABLE = 'sq_research_retention_meta'
# Install update synchronization first: an interrupted initializer must never
# leave insert-only metadata that later source updates can silently invalidate.
TRIGGERS = ('sq_research_retention_update_v1', 'sq_research_retention_insert_v1')
MAX_BYTES = 16384
JOIN = (f'{TABLE} m ON m.source_seq=r.seq AND m.namespace=r.namespace '
        'AND m.source_updated_at=r.updated_at AND m.version=1')


def projection(source='payload'):
    from .retention import _projection
    return _projection('research_events').replace('JSON_EXTRACT(payload,', f'JSON_EXTRACT({source},')


def install(repo):
    repo._execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        source_seq BIGINT UNSIGNED NOT NULL PRIMARY KEY,
        namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
        source_updated_at DATETIME(6) NOT NULL,
        version INT NOT NULL,
        metadata JSON NOT NULL,
        KEY ix_namespace (namespace),
        CONSTRAINT fk_research_retention_source FOREIGN KEY (source_seq)
            REFERENCES pq_research_events(seq) ON DELETE CASCADE ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
    existing = repo._execute(
        'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s '
        'AND TRIGGER_NAME IN (%s,%s)', (repo.config.database, *TRIGGERS), fetch='all')
    existing = {row[0] for row in existing}
    metadata = projection('NEW.payload')
    insert = (f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,metadata) '
              f'SELECT NEW.seq,NEW.namespace,NEW.updated_at,1,{metadata} '
              f'WHERE JSON_STORAGE_SIZE({metadata})<={MAX_BYTES}')
    for name, event in zip(TRIGGERS, ('UPDATE', 'INSERT')):
        if name in existing:
            continue
        body = insert if event == 'INSERT' else (
            f'BEGIN DELETE FROM {TABLE} WHERE source_seq=NEW.seq; {insert}; END')
        try:
            repo._execute(f'CREATE TRIGGER {name} AFTER {event} ON pq_research_events FOR EACH ROW {body}')
        except Exception as exc:
            # Optional optimization: restricted accounts / untrusted binary-log
            # trigger creation must keep the existing fully checked path usable.
            if getattr(exc, 'code', None) in {1142, 1227, 1419}:
                logging.getLogger(__name__).warning('정리 요약 자동 갱신 권한 없음: 기존 보존 검사로 처리합니다.')
                repo._research_retention_metadata = False
                return
            if getattr(exc, 'code', None) != 1359:  # concurrent initializer won
                raise
    repo._research_retention_metadata = True


def available(repo):
    cached = getattr(repo, '_research_retention_metadata', None)
    if cached is not None:
        return cached
    row = repo._execute(
        'SELECT EXISTS(SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s) '
        'AND (SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s '
        'AND TRIGGER_NAME IN (%s,%s))=2',
        (repo.config.database, TABLE, repo.config.database, *TRIGGERS), fetch='one')
    repo._research_retention_metadata = bool(row and row[0])
    return repo._research_retention_metadata


def supports(repo, kind):
    return kind == 'research_events' and hasattr(repo, 'retention_metadata_available') and repo.retention_metadata_available()


def predicate(condition):
    return condition.replace("payload->>'$.kind'", "m.metadata->>'$._record_kind'")


def read_query(repo, keys, condition):
    placeholders = ','.join('%s' for _ in keys)
    return (
        'r.seq,r.captured_at,r.created_at,r.updated_at,'
        f'm.metadata,({predicate(condition)}) FROM pq_research_events r FORCE INDEX (PRIMARY) '
        f'STRAIGHT_JOIN {JOIN} WHERE r.namespace=%s AND r.seq IN ({placeholders})',
        (repo.namespace, *(key[0] for key in keys)))


def read(repo, keys, condition):
    sql, params = read_query(repo, keys, condition)
    return repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ ' + sql, params, fetch='all')


def backfill(repo, rows):
    """At most the already-read small payload batch; no table-wide migration.

    Explicit source locks serialize with updates even under READ COMMITTED;
    the following projection sees the current row and its update trigger.
    The source timestamp must still match the record checked by the caller.
    """
    if not rows:
        return
    metadata = projection()
    pairs = ' OR '.join('(seq=%s AND updated_at=%s)' for _ in rows)
    with repo.transaction():
        placeholders = ','.join('%s' for _ in rows)
        repo._execute(
            'SELECT seq FROM pq_research_events FORCE INDEX (PRIMARY) '
            f'WHERE namespace=%s AND seq IN ({placeholders}) FOR UPDATE',
            (repo.namespace, *(row[0] for row in rows)), fetch='all')
        current = repo._execute(
            'SELECT /*+ MAX_EXECUTION_TIME(3000) */ /* retention metadata refresh */ seq,namespace,updated_at,'
            f'{metadata} FROM pq_research_events FORCE INDEX (PRIMARY) '
            f'WHERE namespace=%s AND ({pairs}) AND JSON_STORAGE_SIZE({metadata})<={MAX_BYTES}',
            (repo.namespace, *(value for row in rows for value in (row[0], row[3]))), fetch='all')
        if current:
            values = ','.join('(%s,%s,%s,1,%s)' for _ in current)
            return repo._execute(
                f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,metadata) VALUES {values} '
                'ON DUPLICATE KEY UPDATE namespace=VALUES(namespace),source_updated_at=VALUES(source_updated_at),'
                'version=VALUES(version),metadata=VALUES(metadata)',
                tuple(value for row in current for value in row))



def delete(repo, selected, cutoff, condition):
    placeholders = ','.join('%s' for _ in selected)
    params = (repo.namespace, *selected, cutoff, cutoff, cutoff)
    removed = repo._execute(
        f'DELETE r FROM pq_research_events r FORCE INDEX (PRIMARY) STRAIGHT_JOIN {JOIN} '
        f'WHERE r.namespace=%s AND r.seq IN ({placeholders}) '
        'AND r.captured_at<%s AND r.created_at<%s AND r.updated_at<%s '
        f'AND ({predicate(condition)})', params)
    # Oversized / legacy metadata uses precisely the original predicate. An
    # updated or absent source is still rejected by the original date guards.
    removed += repo._execute(
        f'DELETE FROM pq_research_events WHERE namespace=%s AND seq IN ({placeholders}) '
        f'AND captured_at<%s AND created_at<%s AND updated_at<%s AND ({condition}) '
        f'AND NOT EXISTS (SELECT 1 FROM {TABLE} m WHERE m.source_seq=pq_research_events.seq '
        'AND m.namespace=pq_research_events.namespace AND m.source_updated_at=pq_research_events.updated_at '
        'AND m.version=1)', params)
    return removed
