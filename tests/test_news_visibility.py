"""뉴스 수신을 유료 요청/분류 정답으로 오인하지 않고, 주말에도 저장 상태를 조회한다."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.news.diagnostics import summarize_news,read_news_status
from sessionquant.dashboard.labels import display_rows,market_hours_label

NOW=datetime(2026,11,15,12,tzinfo=timezone.utc)  # 일요일


def receipt(identity='article-1',**changes):
    return {'id':identity,'article_id':identity,'received_at':NOW.isoformat(),
            'skip_reason':'','eligible_for_llm':True,**changes}


def call(identity='call-1',status='SUCCEEDED',**changes):
    return {'id':identity,'llm_call_id':identity,'article_id':'article-1','network_attempted':True,
        'request_started_at':NOW.isoformat(),'status':status,'model':'fixture',
        'normalized_result':{'sentiment':'POSITIVE','events':[{'type':'EARNINGS_BEAT','evidence':'revenue grew'}],
            'reason':'실적 원문 확인','requires_verification':False},**changes}


def test_weekend_collection_requests_cache_and_pending_are_separate():
    ingestions=[receipt(),receipt('duplicate',article_id='article-1',duplicate=True),receipt('old',skip_reason='stale_article')]
    calls=[call(),call('invalid','SCHEMA_VALIDATION_FAILED',validation_error_code='SCHEMA_MISMATCH'),
        call('pending','REQUESTED'),call('unknown','OUTCOME_UNKNOWN'),
        call('cache','CACHE_HIT',network_attempted=False,llm_call_id=None,recorded_at=NOW.isoformat()),
        call('skip','SKIPPED_PRECHECK',network_attempted=False,llm_call_id=None,recorded_at=NOW.isoformat()),
        call('yesterday',request_started_at=(NOW-timedelta(days=2)).isoformat()),
        call('future',request_started_at=(NOW+timedelta(minutes=1)).isoformat())]
    report=summarize_news(ingestions,calls,now=NOW)
    assert report['counts']=={'news_receipts':3,'unique_articles':2,'llm_request_attempts':4,
        'validated_responses':1,'pending_or_unconfirmed':2,'failed_or_rejected':1,'cache_hits':1}
    assert report['pre_llm_exclusions']=={'stale_article':1}
    assert report['classification_accuracy']=='NOT_MEASURED' and report['order_writes']==report['llm_generation_calls']==0
    assert report['latest_calls'][0]['direction']=='POSITIVE'
    assert report['latest_calls'][0]['evidence']==['revenue grew']
    assert '정규장 아님' in market_hours_label(NOW)


def test_provider_in_flight_and_empty_data_never_claim_success():
    report=summarize_news([],[],now=NOW,health={'code':'NO_KEY','credential_fingerprint':'do-not-show'},
        engine={'status':'RUNNING','heartbeat':(NOW-timedelta(minutes=2)).isoformat(),'armed':True},
        schedule={'rss:one':{'status':'FETCHED','last_attempt_at':NOW.isoformat(),
                            'in_flight_until':(NOW+timedelta(minutes=5)).isoformat()}})
    assert report['providers'][0]['state']=='IN_PROGRESS'
    assert not report['engine']['connected'] and report['counts']['validated_responses']==0
    assert 'do-not-show' not in json.dumps(report) and '키가 없어' in report['message']
    assert not summarize_news([],[],now=NOW)['scope']['may_be_truncated']
    assert summarize_news([receipt()],[],now=NOW,ingestion_limit=1)['scope']['may_be_truncated']


def test_repeated_news_status_preserves_records_and_reports_cached_requests_honestly(tmp_path):
    repo=Repository(tmp_path/'news.db',backend='sqlite')
    try:
        repo.record('news_ingestions',receipt())
        repo.record('llm_calls',call())
        before=repo.storage_stats()['namespace_rows']
        assert read_news_status(repo,NOW)==read_news_status(repo,NOW)
        assert repo.storage_stats()['namespace_rows']==before
        assert repo.all('orders')==[] and repo.all('llm_usage')==[]
    finally:repo.close()


def test_skipped_articles_do_not_push_real_llm_requests_out_of_report(tmp_path):
    repo=Repository(tmp_path/'requests.db',backend='sqlite')
    try:
        repo.record('llm_calls',call('success'))
        repo.record('llm_calls',call('failure','EVIDENCE_MISMATCH',validation_error_code='EVIDENCE_QUOTE_MISMATCH',
                                     failed_field='events.evidence'))
        for i in range(510):
            repo.record('llm_calls',call(f'skip-{i}','SKIPPED_PRECHECK',network_attempted=False,
                                        llm_call_id=None,recorded_at=NOW.isoformat()))
        report=read_news_status(repo,NOW)
        assert report['counts']['llm_request_attempts']==2
        assert report['counts']['validated_responses']==report['counts']['failed_or_rejected']==1
        assert report['audit_status_counts']=={'SKIPPED_PRECHECK':500,'SUCCEEDED':1,'EVIDENCE_MISMATCH':1}
        assert report['scope']['may_be_truncated'] and not report['scope']['requests_may_be_truncated']
        assert next(r for r in report['latest_calls'] if r['status']=='EVIDENCE_MISMATCH')['failed_field']=='events.evidence'
        assert not repo.all('orders')
        assert [row['id'] for row in repo.recent_llm_attempts(1)]==['failure']
        for bad in (0,True,10001):
            with pytest.raises(ValueError):repo.recent_llm_attempts(bad)
    finally:repo.close()


def test_human_labels_preserve_numbers_originals_tickers_and_evidence():
    original=[{'symbol':'AAPL','status':'SUCCEEDED','code':'stale_article','count':3,
        '원문 근거':'The report states UNKNOWN_FIELD exactly.',
        '출처':'toss:/api/v1/candles:1m','제한 사유':'quote_stale, relative_volume_unknown_or_low',
        'unexpected_field':{'new_key':'UNKNOWN_NEW_CODE'}}]
    before=deepcopy(original)
    rows,unknown=display_rows(original)
    assert rows[0]['종목']=='AAPL' and rows[0]['건수']==3 and rows[0]['상태']=='분석 완료'
    assert rows[0]['원문 근거']==original[0]['원문 근거'] and rows[0]['출처']=='토스 1분봉'
    assert rows[0]['제한 사유']=='시세 갱신 지연, 평소 대비 거래량 부족·미확인'
    assert unknown and original==before
    assert 'unexpected_field' not in rows[0]
    assert 'new_key' not in str(rows) and 'UNKNOWN_NEW_CODE' not in str(rows)
    tickers,_=display_rows([{'종목':'LOW','symbol':'LOW','수준':'LOW'}])
    assert tickers[0]=={'종목':'LOW','종목 2':'LOW','수준':'낮음'}


def test_news_status_cli_is_read_only_and_saves_new_json(tmp_path,monkeypatch,capsys):
    from sessionquant.cli import main
    import yaml
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    config=tmp_path/'config.yaml';db=tmp_path/'news.db';out=tmp_path/'news-check.json'
    config.write_text(yaml.safe_dump({'database_backend':'sqlite','db_path':str(db),'company_rss_catalog':False}))
    repo=Repository(db,backend='sqlite')
    repo.record('news_ingestions',receipt(received_at=datetime.now(timezone.utc).isoformat()))
    repo.close()
    assert main(['news-status','--config',str(config),'--output',str(out)])==0
    assert json.loads(capsys.readouterr().out)['status']=='SAVED'
    report=json.loads(out.read_text())
    assert report['counts']['news_receipts']==1 and report['counts']['llm_request_attempts']==0
    assert main(['news-status','--config',str(config),'--output',str(out)])!=0


async def test_copied_history_without_backfill_control_is_still_audited(tmp_path,monkeypatch):
    from sessionquant.market import history_commands
    from sessionquant.market.history import HistoryStore,KIND
    from sessionquant.session_config import SessionConfig
    from test_history_selection import prepare
    from test_watchlist_history import NOW as HISTORY_NOW
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return HISTORY_NOW
    monkeypatch.setattr(history_commands,'datetime',Clock)
    config=SessionConfig(database_backend='sqlite',db_path=tmp_path/'history.db',universe_path=tmp_path/'universe.json')
    repo=Repository(config.db_path,backend='sqlite')
    prepare(HistoryStore(repo),'AAPL',now=HISTORY_NOW)
    before=repo.all(KIND)
    monkeypatch.setattr('sessionquant.watchlist.selection.WatchlistManager',lambda *args,**kwargs:
        SimpleNamespace(base_symbols=lambda now:['AAPL'],store=SimpleNamespace(status=lambda now:{'status':'VALID'})))
    report=await history_commands.command(config,'history-status',details=True)
    assert report['backfill']=={} and report['history_quality']['summary']['complete_symbol_days']==20
    assert report['history_quality']['scope']=='next_operating_session_from_stored_selection'
    assert report['history_readiness_preview']['included_symbols']==['AAPL']
    assert repo.get_control(history_commands.PROGRESS) is None and repo.all(KIND)==before
    repo.close()


def test_main_and_news_show_simple_labels_and_pipeline_counts(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from sessionquant.trading_controls import begin_session
    now=datetime.now(timezone.utc)
    path=tmp_path/'ui.db';repo=Repository(path,backend='sqlite')
    begin_session(repo,mode='paper',now=now)
    repo.record('news_ingestions',receipt(received_at=now.isoformat()))
    repo.record('llm_calls',call(request_started_at=now.isoformat()))
    repo.save_state({'broker':{'cash':'100','reserved_cash':'0','equity':'100','positions':{}}})
    repo.set_control('sessionquant_context',{'environment':'PAPER_LOCAL','paper_data_policy':'observed_paper_v1'})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite');monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(path)])
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30).run()
    assert not app.exception and len(app.metric)==8
    assert any(m.label=='모의 총자산' for m in app.metric)
    assert next(m for m in app.metric if m.label=='AI 분석').value=='1건 완료'
    assert any('요청 1건' in r.value for r in app.caption)
    assert not any('PAPER_LOCAL' in r.value or 'observed_paper_v1' in r.value for r in app.caption)
    app.radio(key='dashboard_page').set_value('뉴스·LLM').run()
    assert not app.exception
    assert next(m for m in app.metric if m.label=='응답 검증 통과').value=='1'
    tables=' '.join(frame.value.to_string() for frame in app.dataframe)
    assert '분석 완료' in tables and '실적 호조' in tables and '긍정' in tables
    assert 'SUCCEEDED' not in tables and 'EARNINGS_BEAT' not in tables
