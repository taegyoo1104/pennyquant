"""실제 처리 시계의 기록·내보내기·결정론·분리 평가를 합성 자료로 검증한다."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.models import utc
from sessionquant.research import journal
from sessionquant.research.bundle import export_recording, load_bundle, events, write_json
from sessionquant.research.evaluation import evaluate_recording, split_plan
from sessionquant.research.quality import quality_report
from sessionquant.research.replay import RecordedNews, replay_recording
from sessionquant.session_demo import synthetic_tape
from sessionquant.session_engine import SessionEngine, event_time


@pytest.fixture
def recording(tmp_path, monkeypatch):
    rows, opening = synthetic_tape()
    clock = SimpleNamespace(now=opening-timedelta(seconds=1))
    monkeypatch.setattr(journal,'clock',lambda:clock.now)
    repo = Repository(tmp_path/'observed.db',backend='sqlite')
    universe = UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opening-timedelta(days=1))
    engine = SessionEngine(repo,universe,synthetic=True,record_inputs=True)
    journal.start(repo,engine,source='toss')
    repo.record('universe_history',universe.snapshots[0])
    for now in sorted({event_time(row) for row in rows}):
        clock.now=now
        engine.apply_batch([row for row in rows if event_time(row)==now],now)
    journal.stop(repo)
    bundle = tmp_path/'bundle'
    manifest = export_recording(repo,bundle)
    yield SimpleNamespace(repo=repo,engine=engine,bundle=bundle,manifest=manifest,clock=clock,opening=opening,rows=rows)
    repo.close()


def test_recording_preserves_originals_and_exact_baseline_across_two_replays(recording,tmp_path):
    observed = recording
    first = replay_recording(observed.bundle,tmp_path/'first.db')
    second = replay_recording(observed.bundle,tmp_path/'second.db')
    assert first['state'] == second['state'] == observed.engine.broker.to_state()
    assert len(first['fills']) == 2 and not first['state']['positions']
    resumed = replay_recording(observed.bundle,tmp_path/'first.db',resume=True)
    assert resumed['state']==first['state'] and resumed['frames_this_execution']==0
    assert resumed['live_order_writes']==resumed['llm_api_calls']==0
    with pytest.raises(ValueError):replay_recording(observed.bundle,tmp_path/'first.db')
    with pytest.raises(ValueError):replay_recording(observed.bundle,tmp_path/'first.db',resume=True,policy='reference')
    with pytest.raises(ValueError):replay_recording(observed.bundle,tmp_path/'first.db',resume=True,research_settings={'ief_15m_pct':'-.31'})
    assert first['risk_settings']['threshold_version']=='hypothesis_v1'
    copied = [item for item in events(observed.bundle/'events.jsonl') if item['kind']=='engine_batch']
    assert copied[0]['data']['events'][0]['data']['received_at'] == observed.rows[0]['data']['received_at']
    timing = copied[0]['data']['events'][0]['timing']
    assert timing['published_at'] is None and timing['event_at']==observed.rows[0]['data']['timestamp']
    assert timing['received_at']==observed.rows[0]['data']['received_at']
    assert copied[0]['data']['decision_at']==copied[0]['available_at']
    assert observed.manifest['synthetic'] and not observed.manifest['actual_out_of_sample_validated']


def test_shadow_keeps_financial_result_and_unknown_policy_blocks_only_new_entries(recording,tmp_path):
    baseline = replay_recording(recording.bundle,tmp_path/'base.db')
    shadow = replay_recording(recording.bundle,tmp_path/'shadow.db',policy='shadow')
    reference = replay_recording(recording.bundle,tmp_path/'ref.db',policy='reference')
    closed = replay_recording(recording.bundle,tmp_path/'closed.db',policy='fail_closed')
    assert baseline['state']==shadow['state']==reference['state']
    assert closed['fills']==[] and closed['reference_blocks']
    assert closed['reference_state_decisions']['NO_OBSERVATION:samples']>0


def test_integrity_no_overwrite_and_source_db_unchanged(recording,tmp_path):
    before = recording.repo.load_state()
    with pytest.raises(ValueError):export_recording(recording.repo,recording.bundle)
    assert recording.repo.load_state()==before
    manifest_path = recording.bundle/'manifest.json'
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    manifest['recording_starts'][0]['data']['strict_market_data'] = True
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='ID'):load_bundle(recording.bundle)
    manifest_path.write_bytes(original)
    path = recording.bundle/'events.jsonl'
    path.write_text(path.read_text()+'\n')
    with pytest.raises(ValueError,match='해시'):load_bundle(recording.bundle)


def test_jsonl_rejects_out_of_order_and_future_data(tmp_path):
    at=datetime(2026,11,13,17,tzinfo=timezone.utc)
    def row(now,received):
        data={'schema_version':1,'kind':'engine_batch','run_id':'SYNTHETIC','source':'fixture','synthetic':True,
            'event_at':None,'published_at':None,'received_at':None,'ingested_at':at.isoformat(),'available_at':now.isoformat(),
            'data':{'decision_at':now.isoformat(),'events':[{'kind':'quote','data':{'timestamp':at.isoformat(),'received_at':received.isoformat()}}]}}
        data['id']='research:'+journal.identity({k:v for k,v in data.items() if k!='ingested_at'})
        return data
    path=tmp_path/'events.jsonl'
    future=row(at,at+timedelta(seconds=1))
    path.write_text(json.dumps(future)+'\n')
    with pytest.raises(ValueError,match='수신 전'):list(events(path))
    path.write_text(json.dumps(row(at+timedelta(seconds=1),at))+'\n'+json.dumps(row(at,at))+'\n')
    with pytest.raises(ValueError,match='역전'):list(events(path))


def test_recording_preserves_analysis_revisions_and_masks_credentials(recording,tmp_path):
    repo=recording.repo
    journal.start(repo,recording.engine,source='toss')
    for direction in ['NEUTRAL','NEGATIVE']:
        repo.record('macro_analysis',{'id':'same-analysis','direction':direction,'analyzed_at':recording.clock.now.isoformat(),
            'api_key':'DO_NOT_SAVE','url':'https://user:password@fixture.invalid/news?token=DO_NOT_SAVE'})
        recording.clock.now+=timedelta(seconds=1)
    journal.stop(repo)
    bundle=tmp_path/'versions';export_recording(repo,bundle)
    rows=[row for row in events(bundle/'events.jsonl') if row['kind']=='macro_analysis']
    assert len(rows)==2 and rows[0]['data']['direction']=='NEUTRAL' and rows[1]['data']['direction']=='NEGATIVE'
    assert 'DO_NOT_SAVE' not in (bundle/'events.jsonl').read_text()
    assert len(repo.all('macro_analysis'))==1


def test_legacy_export_marks_unknown_processing_time_and_cannot_replay(tmp_path):
    repo=Repository(tmp_path/'old.db',backend='sqlite')
    try:
        repo.record('market_bars',{'symbol':'AAPL','timestamp':'2026-11-13T16:00:00+00:00','received_at':'2026-11-13T16:00:01+00:00'})
        output=tmp_path/'legacy'
        manifest=export_recording(repo,output)
        assert manifest['legacy_record_count']==1
        assert json.loads((output/'legacy.jsonl').read_text())['available_at'] is None
        with pytest.raises(ValueError):replay_recording(output,tmp_path/'replay.db')
    finally:repo.close()


@pytest.mark.parametrize('case',['missing_model','missing_prompt','future_analysis','future_context','missing_receipt'])
def test_cached_llm_analysis_never_becomes_available_before_completion(case):
    now=datetime(2026,11,13,17,tzinfo=timezone.utc)
    analysis={'model':'fixture','prompt_version':'v1','published_at':(now-timedelta(minutes=5)).isoformat(),
        'received_at':(now-timedelta(minutes=4)).isoformat(),'analyzed_at':now.isoformat()}
    context={'context':{'status':'AVAILABLE','block_entries':True,'major_adverse':True},'available_at':now.isoformat(),
        'evidence':{'analyses':[analysis]}}
    if case=='missing_model':analysis.pop('model')
    if case=='missing_prompt':analysis.pop('prompt_version')
    if case=='missing_receipt':analysis.pop('received_at')
    if case=='future_analysis':analysis['analyzed_at']=(now+timedelta(seconds=1)).isoformat()
    if case=='future_context':context['available_at']=(now+timedelta(seconds=1)).isoformat()
    provider=RecordedNews();provider.contexts={'AAPL':context}
    assert provider.risk_context(now,'AAPL')['status']=='UNKNOWN'
    assert not provider.risk_context(now,'AAPL')['major_adverse']


def test_walk_forward_final_holdout_is_disjoint_and_never_tunes_thresholds():
    dates=[f'2026-10-{day:02}' for day in range(1,31)]
    plan=split_plan(dates,train_days=10,validation_days=5,holdout_days=5)
    assert plan['status']=='PLANNED' and len(plan['folds'])==3
    held=set(plan['holdout'])
    for fold in plan['folds']:
        assert not held & set(fold['training']+fold['validation'])
        assert max(fold['training'])<min(fold['validation'])<min(held)
    assert split_plan(dates[:20])['status']=='INSUFFICIENT_SESSIONS'


def test_evaluation_reports_all_strategies_ablation_cost_unknown_and_no_performance_claim(recording,tmp_path):
    path=tmp_path/'report.json'
    result=evaluate_recording(recording.bundle,path,min_trades=1)
    assert len(result['results']['diagnostic_all'])==20
    assert result['split_plan']['status']=='INSUFFICIENT_SESSIONS'
    assert result['results']['diagnostic_all']['pullback:reference']['risk_settings']['ief_15m_pct']=='-0.30'
    assert result['recommended_parameters'] is None and not result['actual_out_of_sample_validated']
    assert result['costs']['api_actual_usd'] is None and result['costs']['llm_recorded_verified_usd'] is None
    assert all(row['performance'] is None for row in result['results']['diagnostic_all'].values())
    assert result['proxy_analysis']['pairs']['IEF:DGS10']['status']=='UNVERIFIABLE'
    assert result['live_order_writes']==result['llm_generation_calls']==0
    assert path.with_suffix('.md').exists()
    with pytest.raises(ValueError):evaluate_recording(recording.bundle,path)


def test_capture_transaction_failure_rolls_back_input_frame_and_original_event(recording,monkeypatch):
    repo=recording.repo
    journal.start(repo,recording.engine,source='toss')
    state=repo.load_state();count=len(repo.all('research_events'))
    original=repo.record
    def fail(kind,value):
        if kind=='research_events':raise RuntimeError('기록 장애')
        return original(kind,value)
    monkeypatch.setattr(repo,'record',fail)
    with pytest.raises(RuntimeError):recording.engine.tick(recording.clock.now+timedelta(seconds=1))
    assert repo.load_state()==state and len(repo.all('research_events'))==count
    assert recording.engine.failed


@pytest.mark.parametrize('mode',['delayed','revision','dividend','split'])
def test_reference_recording_keeps_latency_and_action_quality_before_policy_evaluation(tmp_path,monkeypatch,mode):
    from test_macro_reference_risk import view
    rows,opening=synthetic_tape();clock=[opening-timedelta(seconds=1)]
    monkeypatch.setattr(journal,'clock',lambda:clock[0])
    repo=Repository(tmp_path/'observed.db',backend='sqlite')
    universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opening-timedelta(days=1))
    engine=SessionEngine(repo,universe,synthetic=True,record_inputs=True)
    try:
        journal.start(repo,engine,source='toss');repo.record('universe_history',universe.snapshots[0])
        for now in sorted({event_time(row) for row in rows}):
            clock[0]=now
            if now==opening+timedelta(minutes=75,seconds=1):
                reference=view(risk=True,at=now-timedelta(minutes=15) if mode=='delayed' else now)
                if mode=='delayed':
                    for item in reference['items'].values():item['latency_class']='DELAYED'
                else:
                    reference['items']['IEF']['quality_status']='REVISION_PENDING'
                    reference['items']['IEF']['windows']['15']['quality_status']='UNKNOWN'
                repo.set_control('market_reference_view_v1',reference)
            engine.apply_batch([row for row in rows if event_time(row)==now],now)
        journal.stop(repo);bundle=tmp_path/'bundle';export_recording(repo,bundle)
        closed=replay_recording(bundle,tmp_path/'closed.db',policy='fail_closed')
        assert closed['fills']==[] and closed['risk_state_counts']['UNKNOWN']>0
        ref=[r for r in events(bundle/'events.jsonl') if r['kind']=='market_reference_view_v1'][0]
        if mode=='delayed':
            assert utc(ref['data']['items']['IEF']['event_at'])<utc(ref['available_at'])-timedelta(minutes=14)
            assert ref['data']['items']['IEF']['latency_class']=='DELAYED'
        else:assert ref['data']['items']['IEF']['quality_status']=='REVISION_PENDING'
    finally:repo.close()


def test_date_only_fred_never_creates_intraday_benchmark_or_realtime_permission(recording,tmp_path):
    from sessionquant.research.proxies import proxy_report
    repo=recording.repo
    journal.start(repo,recording.engine,source='toss')
    repo.record('fred_reference_observations',{'id':'fred-date','series':'DGS10','observation_date':'2026-11-12',
        'value':'4.2','published_at':None,'point_in_time_verified':False,'received_at':recording.clock.now.isoformat()})
    journal.stop(repo);bundle=tmp_path/'fred';export_recording(repo,bundle)
    result=proxy_report(bundle/'events.jsonl')
    assert result['pairs']['IEF:DGS10']['sample_count']==0
    assert result['pairs']['IEF:DGS10']['correlation'] is None and result['false_signal_rate'] is None


def test_backfilled_bar_uses_receipt_and_processing_time_instead_of_old_candle_time(tmp_path,monkeypatch):
    from sessionquant.models import jsonable,Bar
    now=datetime(2026,11,13,17,tzinfo=timezone.utc)
    monkeypatch.setattr(journal,'clock',lambda:now)
    repo=Repository(tmp_path/'backfill.db',backend='sqlite')
    universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=now-timedelta(days=1))
    engine=SessionEngine(repo,universe,synthetic=True,record_inputs=True)
    try:
        journal.start(repo,engine,source='toss');repo.record('universe_history',universe.snapshots[0])
        old=now-timedelta(days=1)
        raw={'kind':'bar','data':jsonable(Bar('AAPL',old,now,100,101,99,100,100,source='SYNTHETIC_FIXTURE'))}
        engine.apply_batch([raw],now)
        journal.stop(repo);bundle=tmp_path/'backfill';export_recording(repo,bundle)
        frame=next(row for row in events(bundle/'events.jsonl') if row['kind']=='engine_batch')
        assert utc(frame['available_at'])==now and utc(frame['data']['events'][0]['data']['timestamp'])==old
        assert frame['data']['events'][0]['data']['received_at']==now.isoformat()
    finally:repo.close()


def test_recording_run_selection_and_empty_clock_preserve_restart_boundaries(recording,tmp_path):
    repo=recording.repo;recording.clock.now+=timedelta(seconds=1)
    journal.start(repo,recording.engine,source='toss')
    run_id=repo.get_control(journal.CONTROL)['run_id']
    repo.record('universe_history',recording.engine.universe.snapshots[0])
    recording.engine.tick(recording.clock.now)
    journal.stop(repo)
    bundle=tmp_path/'one-run';manifest=export_recording(repo,bundle,run_id=run_id)
    assert len(manifest['recording_starts'])==1 and manifest['recording_starts'][0]['run_id']==run_id
    sample=replay_recording(bundle,tmp_path/'empty-clock.db')
    assert sample['frames_this_execution']==1 and sample['fills']==[]


def test_original_clock_reversal_is_rejected_instead_of_hidden_by_export_sort(recording,tmp_path):
    journal.start(recording.repo,recording.engine,source='toss')
    past=recording.opening+timedelta(minutes=20)
    journal.append(recording.repo,'engine_batch',{'decision_at':past,'events':[]},available_at=past)
    journal.stop(recording.repo)
    with pytest.raises(ValueError,match='역전'):export_recording(recording.repo,tmp_path/'reversal')


def test_cli_quality_and_replay_use_bundle_without_network_or_live_writes(recording,tmp_path,capsys):
    from sessionquant.cli import main
    assert main(['record-quality','--bundle',str(recording.bundle)])==0
    assert 'SYNTHETIC' in capsys.readouterr().out
    assert main(['replay-recording','--bundle',str(recording.bundle),'--db',str(tmp_path/'cli.db')])==0
    assert '"live_order_writes": 0' in capsys.readouterr().out


def test_same_period_news_restriction_applies_only_after_recorded_analysis(tmp_path,monkeypatch):
    rows,opening=synthetic_tape();clock=[opening-timedelta(seconds=1)]
    monkeypatch.setattr(journal,'clock',lambda:clock[0])
    class News:
        def risk_context(self,now,symbol):return {'status':'AVAILABLE','block_entries':True,'major_adverse':False,'blackout':False,'reasons':['합성 검증된 사건']}
        def recorded_evidence(self,now,symbol):return {'analyses':[{'id':'fixture','model':'fixture','prompt_version':'v1',
            'published_at':(now-timedelta(minutes=2)).isoformat(),'received_at':(now-timedelta(minutes=1)).isoformat(),'analyzed_at':now.isoformat()}]}
    repo=Repository(tmp_path/'news.db',backend='sqlite');universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opening-timedelta(days=1))
    engine=SessionEngine(repo,universe,synthetic=True,record_inputs=True,risk_provider=News())
    try:
        journal.start(repo,engine,source='toss');repo.record('universe_history',universe.snapshots[0])
        for now in sorted({event_time(row) for row in rows}):
            clock[0]=now;engine.apply_batch([row for row in rows if event_time(row)==now],now)
        journal.stop(repo);bundle=tmp_path/'news';export_recording(repo,bundle)
        news=replay_recording(bundle,tmp_path/'with-news.db')
        technical=replay_recording(bundle,tmp_path/'without-news.db',use_news=False)
        assert news['fills']==[] and len(technical['fills'])==2
        assert news['missing_news_contexts']==0
    finally:repo.close()


def test_delayed_or_missing_news_evidence_cannot_clear_previously_verified_restriction():
    now=datetime(2026,11,13,17,tzinfo=timezone.utc);provider=RecordedNews()
    evidence={'analyses':[{'model':'fixture','prompt_version':'v1','published_at':(now-timedelta(minutes=2)).isoformat(),
        'received_at':(now-timedelta(minutes=1)).isoformat(),'analyzed_at':now.isoformat()}]}
    provider.contexts={'AAPL':{'context':{'status':'AVAILABLE','block_entries':True,'major_adverse':True},
        'evidence':evidence,'available_at':now.isoformat()}}
    assert provider.risk_context(now,'AAPL')['block_entries']
    provider.contexts={}
    later=provider.risk_context(now+timedelta(seconds=1),'AAPL')
    assert later['status']=='UNKNOWN' and later['block_entries'] and not later['major_adverse']


def test_three_session_holdout_plan_runs_with_warmup_and_no_future_cleanup(tmp_path,monkeypatch):
    from sessionquant.models import Quote,jsonable
    clock=[datetime(2026,11,23,14,30,tzinfo=timezone.utc)]
    monkeypatch.setattr(journal,'clock',lambda:clock[0])
    repo=Repository(tmp_path/'calendar.db',backend='sqlite');universe=UniverseStore()
    universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=clock[0]-timedelta(days=1))
    engine=SessionEngine(repo,universe,synthetic=True,record_inputs=True)
    try:
        journal.start(repo,engine,source='toss');repo.record('universe_history',universe.snapshots[0])
        for day in [23,24,27]:
            clock[0]=datetime(2026,11,day,17,tzinfo=timezone.utc)
            raw={'kind':'quote','data':jsonable(Quote('AAPL',clock[0],clock[0],100,100.01,100,100))}
            engine.apply_batch([raw],clock[0])
        journal.stop(repo);bundle=tmp_path/'calendar';export_recording(repo,bundle)
        quality=quality_report(bundle/'events.jsonl')
        assert quality['trade_dates']==['2026-11-23','2026-11-24','2026-11-27']
        assert next(r for r in quality['symbol_session_coverage'] if r['session_date']=='2026-11-27')['expected_minutes']==150
        result=evaluate_recording(bundle,tmp_path/'holdout.json',strategy='pullback',train_days=1,validation_days=1,holdout_days=1)
        assert set(result['results'])=={'diagnostic_all','validation_1','final_holdout'}
        assert result['split_plan']['holdout']==['2026-11-27']
        assert all(row['diagnostic_simulation']['trades']==0 for row in result['results']['final_holdout'].values())
        assert all('insufficient_trades' in row['blocking_reasons'] for row in result['results']['final_holdout'].values())
    finally:repo.close()


def test_date_only_and_mixed_benchmark_sources_are_not_silently_aligned(recording,tmp_path):
    from sessionquant.research.proxies import proxy_report
    repo=recording.repo;journal.start(repo,recording.engine,source='toss')
    repo.record('market_reference_observations',{'id':'unverified-split','symbol':'IEF','stream':'daily','price':'50',
        'event_at':'2026-11-12T05:00:00+00:00','series_comparability':'UNKNOWN','quality_status':'REVISION_PENDING'})
    repo.record('fred_reference_observations',{'id':'date-only','series':'DGS10','value':'4','observation_date':'2026-11-12',
        'published_at':None,'point_in_time_verified':False})
    journal.stop(repo);bundle=tmp_path/'proxies';export_recording(repo,bundle)
    result=proxy_report(bundle/'events.jsonl')
    assert result['quality_rejected_observations']==1 and not result['pairs']['IEF:DGS10']['samples']


def test_source_mixing_is_reported_without_changing_original_bar_fields(recording,tmp_path):
    journal.start(recording.repo,recording.engine,source='toss')
    recording.clock.now+=timedelta(seconds=1)
    for source in ['FIRST_PROVIDER','SECOND_PROVIDER']:
        raw=deepcopy(recording.rows[0]);raw['data']['source']=source
        raw['data']['received_at']=recording.clock.now.isoformat()
        recording.engine.apply_batch([raw],recording.clock.now)
        recording.clock.now+=timedelta(seconds=1)
    journal.stop(recording.repo);bundle=tmp_path/'mixed';export_recording(recording.repo,bundle)
    assert 'market_source_unknown_or_mixed' in quality_report(bundle/'events.jsonl')['blocking_reasons']


def test_published_daily_pairs_measure_unitless_error_and_decoupling_without_rate_conversion(tmp_path,monkeypatch):
    from sessionquant.research.proxies import proxy_report
    now=[datetime(2026,11,9,22,tzinfo=timezone.utc)]
    monkeypatch.setattr(journal,'clock',lambda:now[0])
    repo=Repository(tmp_path/'pairs.db',backend='sqlite')
    engine=SessionEngine(repo,UniverseStore(),synthetic=True)
    try:
        journal.start(repo,engine,source='toss')
        for offset,(price,yield_value) in enumerate([('100','4'),('99','4.1'),('101','4.2'),('100','4.0')]):
            now[0]=datetime(2026,11,9+offset,22,tzinfo=timezone.utc)
            day=f'2026-11-{9+offset:02}'
            repo.record('market_reference_observations',{'id':'price-'+day,'symbol':'IEF','stream':'daily','price':price,
                'source':'toss:market_reference:v1.2.28','event_at':day+'T05:00:00+00:00',
                'series_comparability':'VERIFIED','quality_status':'GOOD'})
            repo.record('fred_reference_observations',{'id':'yield-'+day,'series':'DGS10','value':yield_value,
                'source':'FRED','observation_date':day,'published_at':day+'T21:00:00+00:00','point_in_time_verified':True})
        journal.stop(repo);bundle=tmp_path/'pairs';export_recording(repo,bundle)
        pair=proxy_report(bundle/'events.jsonl')['pairs']['IEF:DGS10']
        assert pair['sample_count']==3 and pair['correlation'] is not None
        assert pair['standardized_direction_error_rmse'] is not None and pair['decoupling_cases']
        assert pair['price_to_benchmark_conversion'] is None and pair['intraday_validation']=='UNVERIFIABLE'
        assert pair['samples'][0]['benchmark_unit']=='basis_points'
    finally:repo.close()


def test_existing_backtest_report_is_not_overwritten(tmp_path):
    from sessionquant.session_backtest import compare_session
    path=tmp_path/'existing.json';path.write_text('원본 결과')
    with pytest.raises(ValueError):compare_session(tmp_path/'unused.jsonl',tmp_path/'unused-universe.json',path)
    assert path.read_text()=='원본 결과'


@pytest.mark.parametrize('status,verified,count',[('RUMOR',True,0),('CONFIRMED',False,0),('CONFIRMED',True,1)])
def test_news_fact_confirmation_rate_is_separate_from_source_verification(tmp_path,monkeypatch,status,verified,count):
    now=datetime(2026,11,13,17,tzinfo=timezone.utc)
    monkeypatch.setattr(journal,'clock',lambda:now)
    repo=Repository(tmp_path/'news-rate.db',backend='sqlite')
    try:
        journal.start(repo,SessionEngine(repo,UniverseStore(),synthetic=True),source='toss')
        repo.record('macro_analysis',{'id':'fact','status':status,'source_verified':verified,'requires_verification':False,
            'model':'fixture','prompt_version':'v1','analyzed_at':now.isoformat()})
        journal.stop(repo);bundle=tmp_path/'bundle';export_recording(repo,bundle)
        report=quality_report(bundle/'events.jsonl')
        assert report['news_fact_confirmed_count']==count and report['news_fact_confirmed_rate']==count
        assert report['news_source_verified_count']==int(verified)
    finally:repo.close()
