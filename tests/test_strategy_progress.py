from dataclasses import replace
from datetime import timedelta

from sessionquant.market.simple_pullback import progress
from sessionquant.dashboard.focus_summary import focus_rows
from test_strategy_lab import data


def test_progress_counts_current_correction_and_resets_on_latest_upper_reversal():
    original,now=data()
    assert progress(original)['stage']=='REBOUND'
    assert progress(original)['correction_bars']==2
    two=replace(original,five=original.five[:-1])
    one=replace(original,five=original.five[:-2])
    assert progress(two)['stage']=='CORRECTION' and progress(two)['correction_bars']==2
    assert progress(one)['stage']=='CORRECTION' and progress(one)['correction_bars']==1
    reversed_upper=replace(original,fifteen=(original.fifteen[-1],
        replace(original.fifteen[-1],timestamp=original.fifteen[-1].timestamp+timedelta(minutes=15),
                low=original.fifteen[-1].close-2,close=original.fifteen[-1].close-1)))
    assert progress(reversed_upper)['stage']=='TREND_MISSING'


def test_dashboard_invalidates_old_approved_stage_at_new_bar_boundary():
    original,now=data()
    p=progress(original)
    view={'status':'VALID','updated_at':now.isoformat(),'items':[{
        'ticker':'AAPL','focused':True,'last_price':'100','last_price_at':now.isoformat()}]}
    saved={'rows':[{'symbol':'AAPL','evaluated_at':now.isoformat(),'progress':p,
                   'order_status':'APPROVED','signal_present':True}]}
    first=focus_rows(view,now,assessments=saved)[0]
    assert '반등 확인' in first['눌림목 단계'] and '체결은 별도' in first['주문 판단']
    next_bar=now.replace(second=0)+timedelta(minutes=5)
    view['updated_at']=next_bar.isoformat()
    view['items'][0]['last_price_at']=next_bar.isoformat()
    saved['rows'][0]['evaluated_at']=next_bar.isoformat()  # Even a fresh evaluation cannot renew old bars.
    second=focus_rows(view,next_bar,assessments=saved)[0]
    assert second['눌림목 단계']=='판단 자료 갱신 대기'
    assert '미확인' in second['주문 판단']
    assert focus_rows(view,next_bar,assessments={'rows':[]})[0]['눌림목 단계']=='판단 자료 갱신 대기'


def test_engine_heartbeat_and_restore_preserve_original_display_evaluation(tmp_path):
    from sessionquant.database.repository import Repository
    from sessionquant.market.universe import UniverseStore
    from sessionquant.session_engine import SessionEngine,event_time
    from sessionquant.session_demo import synthetic_tape
    from types import SimpleNamespace
    tape,opened=synthetic_tape()
    now=opened+timedelta(minutes=75,seconds=1)
    universe=UniverseStore();universe.refresh(['AAPL'],source='SYNTHETIC_FIXTURE',observed_at=opened-timedelta(days=1))
    repo=Repository(tmp_path/'progress.db',backend='sqlite')
    try:
        engine=SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,use_market_filter=False,
            candidate_settings={'enabled':True,'use_sector_score':False})
        engine.apply_batch([r for r in tape if event_time(r)<=now],now)
        before=repo.get_control('sessionquant_context')['strategy_lab']
        assert before['rows'][0]['progress']['stage']=='REBOUND'
        engine.apply_batch([],now+timedelta(seconds=31))
        assert repo.get_control('sessionquant_context')['strategy_lab']==before
        # The same provider used by scheduled focus reads received bars, not UI state.
        policy=SimpleNamespace(pattern_provider=None)
        restarted=SessionEngine(repo,universe,strategy='pullback_simple',synthetic=True,restore=True,
            use_market_filter=False,watchlist_policy=policy,
            candidate_settings={'enabled':True,'use_sector_score':False})
        assert restarted._lab_rows==before['rows']
        assert policy.pattern_provider('AAPL',now)['stage']=='REBOUND'
    finally:repo.close()
