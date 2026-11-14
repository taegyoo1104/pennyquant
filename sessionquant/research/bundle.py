"""작성자를 중지한 원장에서 새 연구 묶음을 내보내고 해시·시점을 검사한다."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from ..models import utc
from ..portfolio.models import serialize
from .journal import clean, identity


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(serialize(value), stream, ensure_ascii=False, indent=2, allow_nan=False)


def file_hash(path):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    return digest.hexdigest()


def events(path):
    """순서 역전·변조·미래 입력을 거부하며 파일 전체를 메모리에 올리지 않는다."""
    last = None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip(): continue
            if len(line) > 16_000_000: raise ValueError('연구 이벤트 크기 제한을 초과했습니다.')
            row = json.loads(line)
            if row.get('schema_version') != 1: raise ValueError('연구 이벤트 버전을 확인하세요.')
            stamp = utc(row['available_at'])
            if last and stamp < last: raise ValueError('연구 이벤트의 사용 가능 시각이 역전됐습니다.')
            last = stamp
            canonical = {key: value for key, value in row.items() if key not in {'id', 'ingested_at', 'source_sequence'}}
            if row['id'] != 'research:'+identity(canonical): raise ValueError('연구 이벤트 내용 해시가 다릅니다.')
            if row['kind'] == 'engine_batch':
                data = row['data']
                if utc(data['decision_at']) != stamp: raise ValueError('전략 처리 시각과 기록 시점이 다릅니다.')
                from ..session_engine import event_time
                for raw in data['events']:
                    if event_time(raw) > stamp: raise ValueError('수신 전 자료를 사용하는 전략 기록입니다.')
                    if raw.get('timing',{}).get('available_at') and utc(raw['timing']['available_at']) > stamp:
                        raise ValueError('입력의 실제 사용 가능 시각이 처리 시각보다 미래입니다.')
                    value = raw['data']
                    if raw['kind'] in {'bar', 'quote'} and utc(value['timestamp']) > utc(value['received_at']):
                        raise ValueError('미래 가격을 포함한 기록입니다.')
            yield row


def export_recording(repo, output, *, run_id=None):
    """새 디렉터리만 만들며 원래 원장·입력·식별자는 수정하지 않는다."""
    from .quality import quality_report
    output = Path(output)
    if output.exists(): raise ValueError('기존 내보내기 경로를 사용할 수 없습니다. 새 폴더를 지정하세요.')
    output.mkdir(parents=True)
    snapshots, starts, count = {}, [], 0
    last_decision = None
    # 원장 순번과 논리적 사용 가능 시각을 별도로 유지한다. 정렬은 임시 디스크에서 수행한다.
    with TemporaryDirectory(prefix='sessionquant-export-') as directory:
        index = sqlite3.connect(str(Path(directory)/'sort.db'))
        try:
            index.execute('CREATE TABLE events (available TEXT, seq INTEGER, payload TEXT)')
            for seq, row in repo.iter_records('research_events'):
                if run_id is not None and row['run_id'] != run_id:continue
                stamp = utc(row['available_at']).isoformat()
                if row['kind'] == 'engine_batch':
                    decision = utc(row['data']['decision_at'])
                    if last_decision and decision < last_decision:raise ValueError('원본 전략 처리 시각이 역전됐습니다. 자동 정렬로 숨기지 않습니다.')
                    last_decision = decision
                row = {**row, 'source_sequence': seq}
                index.execute('INSERT INTO events VALUES(?,?,?)', (stamp, seq, json.dumps(row, ensure_ascii=False, allow_nan=False)))
                count += 1
                if row['kind'] == 'recording_start': starts.append(row)
                if row['kind'] == 'universe_history':
                    value = dict(row['data'])
                    value['known_at'] = max(utc(value['known_at']), utc(stamp)).isoformat()
                    snapshots[identity(value)] = value
            index.commit()
            with (output/'events.jsonl').open('x', encoding='utf-8') as stream:
                for (payload,) in index.execute('SELECT payload FROM events ORDER BY available,seq'):
                    stream.write(payload+'\n')
        finally: index.close()
    if run_id is not None and not starts:raise ValueError('지정한 실행 시작 기록이 없습니다.')
    legacy = 0
    if not starts:
        # 최신값으로 덮어쓴 옛 원장에서는 처리 시점을 복원할 수 없다. 원본만 분리 보존한다.
        with (output/'legacy.jsonl').open('x', encoding='utf-8') as stream:
            for kind in ('market_bars','quote_snapshots','stocks','news_events','news_analysis','macro_news',
                         'macro_analysis','universe_history','market_reference_observations','fred_reference_observations'):
                for seq, row in repo.iter_records(kind):
                    stream.write(json.dumps({'kind':kind,'source_sequence':seq,'available_at':None,'data':clean(row)}, ensure_ascii=False)+'\n')
                    legacy += 1
                    if kind == 'universe_history':snapshots[identity(row)] = clean(row)
    write_json(output/'universe.json', {'schema_version':1, 'snapshots':list(snapshots.values())})
    quality = quality_report(output/'events.jsonl')
    if legacy: quality['blocking_reasons'].append('legacy_processing_time_unknown')
    write_json(output/'quality.json', quality)
    names = ['events.jsonl','universe.json','quality.json']+(['legacy.jsonl'] if not starts else [])
    hashes = {name:file_hash(output/name) for name in names}
    manifest = {'schema_version':1,'bundle_id':identity({'files':hashes,'starts':starts,'synthetic':quality['synthetic']}),'files':hashes,'record_count':count,
        'legacy_record_count':legacy,'exported_at':datetime.now(timezone.utc).isoformat(),
        'recording_starts':starts,'synthetic':quality['synthetic'], 'environment':'PAPER_LOCAL',
        'data_validation':'SYNTHETIC' if quality['synthetic'] else 'UNVERIFIED_DATA',
        'actual_out_of_sample_validated':False,'live_order_writes':0,'llm_generation_calls':0}
    write_json(output/'manifest.json',manifest)
    return manifest


def load_bundle(path):
    path = Path(path)
    manifest = json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('environment') != 'PAPER_LOCAL':
        raise ValueError('지원하지 않는 연구 묶음입니다.')
    for name, digest in manifest['files'].items():
        if name not in {'events.jsonl','universe.json','quality.json','legacy.jsonl'}:
            raise ValueError('연구 파일 이름을 확인하세요.')
        if file_hash(path/name) != digest: raise ValueError('연구 파일 해시가 다릅니다. 원본을 다시 내보내세요.')
    if not {'events.jsonl','universe.json','quality.json'} <= set(manifest['files']):
        raise ValueError('필수 연구 파일이 없습니다.')
    if identity({'files':manifest['files'],'starts':manifest['recording_starts'],'synthetic':manifest['synthetic']}) != manifest['bundle_id']: raise ValueError('연구 묶음 ID가 다릅니다.')
    return manifest
