"""Small pipeline list rows, maintained with the source transaction.

Existing payloads are not rewritten or scanned during installation. Recent
legacy rows without a valid summary use the original bounded projection.
"""
import logging

from .pipeline_projection import expression, summary, validate_limit

TABLE = 'sq_pipeline_summary'
TRIGGERS = ('sq_pipeline_summary_update_v1', 'sq_pipeline_summary_insert_v1')
OPTIONAL_ERRORS = {1142, 1143, 1227, 1419}


def _disable(repo):
    repo._pipeline_summary_available = False
    logging.getLogger(__name__).warning('처리 요약 저장소 사용 불가: 기존 제한 조회로 처리합니다.')


def install(repo):
    try:
        repo._execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            source_seq BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            namespace VARCHAR(64) COLLATE utf8mb4_bin NOT NULL,
            source_updated_at DATETIME(6) NOT NULL,
            version INT NOT NULL,
            summary JSON NOT NULL,
            KEY ix_namespace (namespace),
            CONSTRAINT fk_pipeline_summary_source FOREIGN KEY (source_seq)
                REFERENCES pq_pipeline_status(seq) ON DELETE CASCADE ON UPDATE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin''')
        rows = repo._execute(
            'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s '
            'AND TRIGGER_NAME IN (%s,%s)', (repo.config.database, *TRIGGERS), fetch='all')
        existing = {row[0] for row in rows}
        insert = (f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,summary) '
                  f'VALUES(NEW.seq,NEW.namespace,NEW.updated_at,1,{expression("mysql", "NEW.payload")})')
        # Install UPDATE first so a partially installed INSERT trigger cannot
        # leave stale summaries after writes by an older application version.
        for name, event in zip(TRIGGERS, ('UPDATE', 'INSERT')):
            if name in existing:
                continue
            body = insert if event == 'INSERT' else (
                f'BEGIN DELETE FROM {TABLE} WHERE source_seq=NEW.seq; {insert}; END')
            try:
                repo._execute(f'CREATE TRIGGER {name} AFTER {event} ON pq_pipeline_status FOR EACH ROW {body}')
            except Exception as exc:
                if getattr(exc, 'code', None) != 1359:  # another initializer installed it
                    raise
    except Exception as exc:
        if getattr(exc, 'code', None) not in OPTIONAL_ERRORS:
            raise
        _disable(repo)
        return
    repo._pipeline_summary_available = True


def available(repo):
    cached = getattr(repo, '_pipeline_summary_available', None)
    if cached is not None:
        return cached
    try:
        row = repo._execute(
            'SELECT EXISTS(SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s) '
            'AND (SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s '
            'AND TRIGGER_NAME IN (%s,%s))=2',
            (repo.config.database, TABLE, repo.config.database, *TRIGGERS), fetch='one')
    except Exception as exc:
        if getattr(exc, 'code', None) not in OPTIONAL_ERRORS:
            raise
        _disable(repo)
        return False
    repo._pipeline_summary_available = bool(row and row[0])
    return repo._pipeline_summary_available


def recent(repo, limit):
    validate_limit(limit)
    if not available(repo):
        rows = repo._execute(f"SELECT {expression('mysql')} FROM pq_pipeline_status FORCE INDEX (ix_namespace_seq) "
            "WHERE namespace=%s ORDER BY seq DESC LIMIT %s", (repo.namespace, limit), fetch='all')
        return [summary(row[0]) for row in reversed(rows)]

    # Select the display window before joining. Old, missing, or invalid
    # summaries cannot cause a scan of the entire source history.
    keys = repo._execute('SELECT seq FROM pq_pipeline_status FORCE INDEX (ix_namespace_seq) '
        'WHERE namespace=%s ORDER BY seq DESC LIMIT %s', (repo.namespace, limit), fetch='all')
    if not keys:
        return []
    ids = [row[0] for row in keys]
    placeholders = ','.join('%s' for _ in ids)
    try:
        rows = repo._execute(
            f'SELECT r.seq,m.summary FROM pq_pipeline_status r FORCE INDEX (PRIMARY) '
            f'STRAIGHT_JOIN {TABLE} m ON m.source_seq=r.seq AND m.namespace=r.namespace '
            'AND m.source_updated_at=r.updated_at AND m.version=1 '
            f'WHERE r.namespace=%s AND r.seq IN ({placeholders})', (repo.namespace, *ids), fetch='all')
    except Exception as exc:
        if getattr(exc, 'code', None) not in {1142, 1143, 1146}:
            raise
        _disable(repo)
        rows = []
    found = dict(rows)
    missing = [seq for seq in ids if seq not in found]
    if missing:
        placeholders = ','.join('%s' for _ in missing)
        found.update(repo._execute(
            f"SELECT seq,{expression('mysql')} FROM pq_pipeline_status FORCE INDEX (PRIMARY) "
            f'WHERE namespace=%s AND seq IN ({placeholders})', (repo.namespace, *missing), fetch='all'))
    return [summary(found[seq]) for seq in reversed(ids) if seq in found]


def backfill_page(repo, ids):
    """Explicit maintenance only: derive at most ten missing summaries.

    Source locks keep direct SQL/old writers consistent with this projection;
    already current summaries are never projected from the large source body.
    """
    ids = list(dict.fromkeys(ids))
    if not ids or len(ids) > 10 or any(type(seq) is not int or seq < 1 for seq in ids):
        raise ValueError('처리 요약 보충 범위를 확인하세요.')
    if not available(repo):
        raise ValueError('처리 요약 트리거를 확인할 수 없습니다.')
    placeholders = ','.join('%s' for _ in ids)
    with repo.transaction():
        repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ seq FROM pq_pipeline_status FORCE INDEX (PRIMARY) '
            f'WHERE namespace=%s AND seq IN ({placeholders}) FOR UPDATE', (repo.namespace, *ids), fetch='all')
        rows = repo._execute('SELECT /*+ MAX_EXECUTION_TIME(3000) */ r.seq,r.namespace,r.updated_at,'
            f'{expression("mysql", "r.payload")} FROM pq_pipeline_status r FORCE INDEX (PRIMARY) '
            f'WHERE r.namespace=%s AND r.seq IN ({placeholders}) AND NOT EXISTS '
            f'(SELECT 1 FROM {TABLE} m WHERE m.source_seq=r.seq AND m.namespace=r.namespace '
            'AND m.source_updated_at=r.updated_at AND m.version=1)', (repo.namespace, *ids), fetch='all')
        if rows:
            values = ','.join('(%s,%s,%s,1,%s)' for _ in rows)
            repo._execute(f'INSERT INTO {TABLE}(source_seq,namespace,source_updated_at,version,summary) '
                f'VALUES {values} ON DUPLICATE KEY UPDATE namespace=VALUES(namespace),'
                'source_updated_at=VALUES(source_updated_at),version=1,summary=VALUES(summary)',
                tuple(value for row in rows for value in row))
        return len(rows)
