"""Review a logical Paper backup in an isolated disposable MySQL, without starting the app.

Only summary fields are exported. No credentials, raw news, account identifiers, SQL,
or unrestricted exception messages are included in the report. Requires Python 3.10+
and Docker. No third party Python packages or GCP VM access are needed.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
TABLES = ('pipeline_status', 'orders', 'order_intents', 'executions', 'signals', 'system_logs', 'llm_calls', 'research_events')
CONTROLS = ('startup_data_warmup_v1', 'watchlist_dashboard_v1', 'watchlist_history_selection_v1',
            'trade_control_state', 'watchlist_scan_state_v1')


def stamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def korean(value):
    value = stamp(value)
    return value.astimezone(KST).isoformat() if value else None


def code(value):
    value = str(value)
    return value if re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) and '://' not in value else 'REDACTED'


def symbols(values):
    return sorted({v for v in values if isinstance(v, str) and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', v)})


def reasons(values):
    counts = collections.Counter()
    for value in values:
        if isinstance(value, str): counts[code(value)] += 1
    return dict(counts)


def classify_error(row):
    text = str(row.get('message', '')) + ' ' + str(row.get('detail', ''))
    lowered = text.lower()
    for pattern, label in [('no space left', 'storage_full'), ('2006', 'mysql_disconnected'),
                           ('2013', 'mysql_connection_lost'), ('timeout', 'timeout'),
                           ('429', 'rate_limited'), ('403', 'access_denied')]:
        if pattern in lowered: return label
    return code(row.get('error_type') or 'message_omitted')


class Review:
    def __init__(self, since, until, tables):
        self.since, self.until = since, until
        self.tables, self.spaces = set(tables), {}

    def space(self, name):
        key = code(name)
        return self.spaces.setdefault(key, {
            'counts_in_window': {}, 'records_without_time': {}, 'time_basis_counts': {}, 'latest_controls': {},
            'focus_snapshots': [], 'activity': {}, 'log_summary': {}, 'log_samples': [],
            'research': {'kinds': {}, 'engine_first_kst': None, 'engine_last_kst': None,
                         'engine_batches': 0, 'running_batches': 0, 'decision_count': 0,
                         'scope_blockers': {}},
        })

    def consume(self, row):
        item = self.space(row['namespace'])
        data = row.get('data') or {}
        kind = row['kind']
        if kind == 'controls':
            self.control(item, row['name'], data, row.get('captured_at'))
            return
        time_keys = {'research_events': ('available_at',), 'pipeline_status': ('known_at', 'timestamp', 'observed_at'),
                     'orders': ('created_at', 'timestamp'), 'order_intents': ('created_at', 'timestamp')}
        keys = time_keys.get(kind, ('timestamp', 'received_at', 'created_at', 'attempted_at', 'analyzed_at'))
        payload_at = next((stamp(data[k]) for k in keys if stamp(data.get(k))), None)
        at = payload_at or stamp(row.get('captured_at'))
        if at is None:
            item['records_without_time'][kind] = item['records_without_time'].get(kind, 0) + 1
            return
        if not self.since <= at < self.until: return
        basis = kind + (':payload_time' if payload_at else ':database_capture_time')
        item['time_basis_counts'][basis] = item['time_basis_counts'].get(basis, 0) + 1
        item['counts_in_window'][kind] = item['counts_in_window'].get(kind, 0) + 1
        if kind == 'pipeline_status' and data.get('kind') == 'watchlist_focus':
            names = symbols(x.get('ticker', x.get('symbol')) for x in data.get('items', []) if isinstance(x, dict))
            item['focus_snapshots'].append({'at_kst': korean(at), 'count': len(names), 'symbols': names,
                                            'data_status': code(data.get('data_status', 'UNKNOWN'))})
        elif kind == 'research_events':
            research = item['research']; subtype = code(data.get('kind'))
            research['kinds'][subtype] = research['kinds'].get(subtype, 0) + 1
            if subtype == 'engine_batch':
                body = data.get('data') or {}
                research['engine_batches'] += 1
                when = korean(at)
                research['engine_first_kst'] = min(research['engine_first_kst'] or when, when)
                research['engine_last_kst'] = max(research['engine_last_kst'] or when, when)
                research['running_batches'] += int((body.get('control_state') or {}).get('running') is True)
                research['decision_count'] += int(body.get('decision_count') or 0)
                for blockers in (body.get('scope_blockers') or {}).values():
                    for reason, count in reasons(blockers or []).items():
                        research['scope_blockers'][reason] = research['scope_blockers'].get(reason, 0) + count
        elif kind == 'system_logs':
            level = code(data.get('level', 'UNKNOWN'))
            label = classify_error(data) if level in {'ERROR', 'WARNING', 'CRITICAL'} else 'informational'
            key = level + ':' + label
            item['log_summary'][key] = item['log_summary'].get(key, 0) + 1
            if level in {'ERROR', 'WARNING', 'CRITICAL'}:
                item['log_samples'].append({'at_kst': korean(at), 'level': level, 'category': label,
                                            'error_type': code(data.get('error_type', 'UNKNOWN'))})
                item['log_samples'] = item['log_samples'][-30:]
        elif kind in {'orders', 'executions', 'order_intents', 'signals', 'llm_calls'}:
            group = item['activity'].setdefault(kind, {'first_kst': None, 'last_kst': None, 'statuses': {}, 'samples': []})
            when = korean(at)
            group['first_kst'] = min(group['first_kst'] or when, when)
            group['last_kst'] = max(group['last_kst'] or when, when)
            status = code(data.get('status', 'UNKNOWN'))
            group['statuses'][status] = group['statuses'].get(status, 0) + 1
            intent = data.get('intent') or {}
            ticker = symbols([data.get('symbol', intent.get('symbol'))])
            group['samples'].append({'at_kst': when, 'symbol': ticker[0] if ticker else None,
                                     'side': code(data.get('side', intent.get('side', 'UNKNOWN'))), 'status': status})
            group['samples'] = group['samples'][-30:]

    def control(self, item, name, data, captured):
        if name not in CONTROLS: return
        saved = stamp(captured)
        view = {'saved_at_kst': korean(captured), 'latest_only': True,
                'saved_in_window': bool(saved and self.since <= saved < self.until)}
        for key in ('started_at', 'finished_at', 'updated_at', 'heartbeat', 'checked_at', 'last_cycle_at'):
            if data.get(key): view[key + '_kst'] = korean(data[key])
        for key in ('status', 'phase', 'session'):
            if key in data: view[key] = code(data[key])
        for key in ('completed_tasks', 'complete_tasks', 'required_tasks', 'received_pages',
                    'reused_history_tasks', 'history_total_tasks', 'history_ready_count', 'history_excluded_count',
                    'included_count', 'excluded_count', 'operating_count', 'focus_count', 'last_cycle_calls',
                    'cycle_call_budget', 'last_cycle_duration_seconds', 'budget_exhausted', 'running', 'data_ready'):
            if isinstance(data.get(key), (bool, int, float)): view[key] = data[key]
        if name == 'startup_data_warmup_v1':
            view['issues_by_reason'] = reasons(x.get('reason') for x in data.get('issues', []) if isinstance(x, dict))
        if name == 'watchlist_dashboard_v1':
            view['entry_blockers'] = reasons(reason for x in data.get('items', []) for reason in x.get('entry_blockers', []))
            view['light_data_status'] = dict(collections.Counter(code(x.get('light_data_status')) for x in data.get('items', [])))
        if name == 'watchlist_scan_state_v1':
            view['read_measurements'] = [{'source': code(x.get('source')), 'symbol': symbols([x.get('symbol')]),
                 'status': code(x.get('status')), 'duration_seconds': x.get('duration_seconds'),
                 'at_kst': korean(x.get('received_at') or x.get('timestamp'))} for x in data.get('read_measurements', [])[-60:]]
        item['latest_controls'][name] = view

    def finish(self):
        for item in self.spaces.values():
            for kind in TABLES:
                if 'pq_' + kind in self.tables: item['counts_in_window'].setdefault(kind, 0)
            snapshots = sorted(item.pop('focus_snapshots'), key=lambda x: x['at_kst'])
            changes = []
            for snap in snapshots:
                if not changes or (snap['symbols'], snap['data_status']) != (changes[-1]['symbols'], changes[-1]['data_status']):
                    changes.append(snap)
            item['focus'] = {'snapshot_count': len(snapshots), 'first': snapshots[0] if snapshots else None,
                             'last': snapshots[-1] if snapshots else None,
                             'max_count': max((s['count'] for s in snapshots), default=None),
                             'first_nonzero': next((s for s in snapshots if s['count']), None),
                             'changes': changes[:100], 'changes_total': len(changes)}
        return {'since_kst': korean(self.since), 'until_kst_exclusive': korean(self.until),
                'missing_tables': sorted({'pq_' + x for x in TABLES} - self.tables), 'namespaces': self.spaces,
                'limitations': ['Controls are latest snapshots, not a complete warmup timeline.',
                    'Order rows describe retained order records, not every status transition.',
                    'Missing tables or unparseable timestamps do not prove no activity.',
                    'Errors absent from DB (including OS/container logs) require separate journal inspection.',
                    'No engine batches can mean recording was disabled; it does not prove the engine never ran.']}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def json_query(expression, source):
    # Send ASCII hex over the mysql CLI pipe, independent of locale/client decoding.
    # Conversion before HEX preserves Korean, accented text, and four-byte Unicode.
    return f'SELECT HEX(CONVERT({expression} USING utf8mb4)) FROM {source};'


def decode_row(line):
    return json.loads(bytes.fromhex(line.decode('ascii').strip()).decode('utf-8'))


def review_backup(backup, since, until):
    name = 'sq-review-' + uuid4().hex[:12]
    created = False
    with tempfile.TemporaryDirectory(prefix='sq-review-') as directory:
        env = Path(directory)/'db.env'; env.write_text('MYSQL_ROOT_PASSWORD='+secrets.token_urlsafe(32)+'\nMYSQL_DATABASE=sessionquant\n'); env.chmod(0o600)
        try:
            run('docker', 'run', '-d', '--name', name, '--network', 'none', '--memory', '1536m', '--cpus', '1',
                '--env-file', str(env), 'mysql:8.4', '--skip-log-bin', '--max-allowed-packet=64M',
                '--innodb-buffer-pool-size=128M', '--performance-schema=OFF')
            created = True
            free_kib = int(run('docker', 'exec', name, 'sh', '-c',
                              "df -Pk /var/lib/mysql | awk 'NR==2 {print $4}'", timeout=30).stdout.strip())
            if free_kib < 2*1024*1024: raise RuntimeError('temporary_disk_needs_2GiB_free')
            mysql = ['docker', 'exec', '-i', name, 'sh', '-c',
                     'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --default-character-set=utf8mb4 --batch --raw --skip-column-names sessionquant']
            deadline = time.monotonic()+180
            while True:
                try:
                    run(*mysql, input=b'SELECT 1;', timeout=10); break
                except subprocess.SubprocessError:
                    if time.monotonic() >= deadline: raise RuntimeError('temporary_mysql_start_failed') from None
                    time.sleep(1)
            print('임시 DB 준비 완료. 백업 복원 중…', flush=True)
            # Read only the SQL member; never extract arbitrary archive paths or app secrets.
            with tarfile.open(backup, 'r:gz') as archive:
                members = [m for m in archive.getmembers() if m.name in {'database.sql.gz', 'database.sql'}]
                if len(members) != 1 or not members[0].isfile(): raise ValueError('backup_sql_member_invalid')
                member = members[0]
                stream = archive.extractfile(member)
                if member.name.endswith('.gz'): stream = gzip.GzipFile(fileobj=stream)
                with tempfile.TemporaryFile() as errors:
                    proc = subprocess.Popen(mysql, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
                    try:
                        with stream:
                            while chunk := stream.read(1024*1024): proc.stdin.write(chunk)
                        proc.stdin.close()
                        if proc.wait(timeout=600): raise RuntimeError('backup_import_failed')
                    finally:
                        if proc.poll() is None: proc.kill(); proc.wait()
            tables = run(*mysql, input=b'SHOW TABLES;', timeout=30).stdout.decode('utf-8').splitlines()
            review = Review(since, until, tables)
            queries = []
            for kind in TABLES:
                table = 'pq_' + kind
                if table not in tables: continue
                payload = 'payload'
                if kind == 'research_events':
                    payload = "JSON_OBJECT('kind',payload->'$.kind','available_at',payload->'$.available_at','data',JSON_OBJECT('control_state',payload->'$.data.control_state','decision_count',JSON_LENGTH(payload->'$.data.decisions'),'scope_blockers',payload->'$.data.scope_blockers'))"
                queries.append(json_query(f"JSON_OBJECT('kind','{kind}','namespace',namespace,'captured_at',captured_at,'data',{payload})", f'{table} ORDER BY seq'))
            if 'pq_controls' in tables:
                names = ','.join("'"+key+"'" for key in CONTROLS)
                queries.append(json_query("JSON_OBJECT('kind','controls','name',name,'namespace',namespace,'captured_at',updated_at,'data',payload)", f'pq_controls WHERE name IN ({names})'))
            sql_path = Path(directory)/'review.sql'; sql_path.write_text('\n'.join(queries), encoding='utf-8')
            print('기록 요약 중…', flush=True)
            with sql_path.open('rb') as source, tempfile.TemporaryFile() as errors:
                proc = subprocess.Popen(mysql, stdin=source, stdout=subprocess.PIPE, stderr=errors)
                try:
                    for line in proc.stdout:
                        if line.strip(): review.consume(decode_row(line))
                    if proc.wait(timeout=60): raise RuntimeError('backup_read_failed')
                finally:
                    if proc.poll() is None: proc.kill(); proc.wait()
            result = review.finish()
            result['read_only_production'] = True
            result['application_started'] = False
            result['provider_calls'] = result['llm_calls_by_reviewer'] = result['order_writes_by_reviewer'] = 0
            result['backup_file'] = backup.name
            with backup.open('rb') as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
            result['backup_sha256'] = digest.hexdigest()
            return result
        finally:
            if created:
                # Unique container and its anonymous volume only. No global prune.
                cleanup = subprocess.run(['docker', 'rm', '-fv', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if cleanup.returncode: print('임시 컨테이너 정리 실패: '+name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    args = parser.parse_args()
    since, until = stamp(args.since), stamp(args.until)
    if not since or not until or since >= until: parser.error('기간을 ISO 형식으로 지정하세요.')
    if not args.backup.is_file(): parser.error('백업 파일이 없습니다.')
    if args.output.exists(): parser.error('결과 파일이 이미 있습니다. 새 이름을 지정하세요.')
    try:
        result = review_backup(args.backup, since, until)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print('점검 완료: '+str(args.output))
        for namespace, data in result['namespaces'].items():
            print(namespace, json.dumps({'counts':data['counts_in_window'], 'focus_max':data['focus']['max_count']}, ensure_ascii=False))
    except Exception as exc:
        # Do not print Docker command env values, SQL, or server exception text.
        known = {'temporary_mysql_start_failed', 'backup_sql_member_invalid', 'backup_import_failed',
                 'backup_read_failed', 'temporary_disk_needs_2GiB_free'}
        reason = str(exc) if str(exc) in known else type(exc).__name__
        raise SystemExit('점검 실패: '+reason+' · 운영 DB는 변경하지 않았습니다.') from None


if __name__ == '__main__':
    main()
