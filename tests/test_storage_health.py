from datetime import datetime, timedelta, timezone
import json

from sessionquant.dashboard.storage_health import load_report, messages

NOW = datetime(2026,11,25,4,tzinfo=timezone.utc)


def test_missing_local_report_is_silent_but_invalid_report_is_unknown(tmp_path):
    p=tmp_path/'storage.json'
    assert load_report(p)=={}
    p.write_text('not json')
    assert load_report(p)=={'status':'UNKNOWN'}
    p.write_text(' '*16385)
    assert load_report(p)=={'status':'UNKNOWN'}


def test_failed_backup_and_skipped_cleanup_cannot_look_complete(tmp_path):
    report={'version':1,'checked_at':NOW.isoformat(),'status':'LOW',
            'backup_status':'FAILED now phase=PACKAGE',
            'retention_status':'SKIPPED now reason=BACKUP_FAILED'}
    p=tmp_path/'storage.json';p.write_text(json.dumps(report))
    result=messages(load_report(p),NOW)
    assert len(result)==3
    assert any('백업 실패로 실행하지 못했습니다' in x for x in result)
    assert any('공간이 부족' in x for x in result)


def test_stale_success_does_not_claim_current_health():
    report={'checked_at':(NOW-timedelta(minutes=5)).isoformat(),'status':'OK'}
    assert '오래됐습니다' in messages(report,NOW)[0]
    report['checked_at']=NOW.isoformat()
    assert messages(report,NOW)==[]
