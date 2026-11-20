"""A ranking change must not abandon a pullback between opening-bell slots."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sessionquant.watchlist.focus_schedule import choose_scheduled
from sessionquant.market.bars import session_bounds
from sessionquant.session_config import SessionConfig
from test_focus_stability import metric
from watchlist_fixtures import NOW, context


def manager():
    return SimpleNamespace(state={'focus':[]},pattern_provider=None,
        config=SessionConfig(strategy='pullback_simple',focus_selection='turnover',focus_size=1,
            focus_rebalance_minutes=30,company_rss_catalog=False))


def select(m, now, a=10, b=100):
    choose_scheduled(m,[metric('A',a,now),metric('B',b,now)],now)
    return [r['ticker'] for r in m.state['focus']]


def pattern(now, stage='CORRECTION'):
    return {'stage':stage,'five_at':now.replace(second=0,microsecond=0).isoformat(),
        'fifteen_at':now.replace(second=0,microsecond=0).isoformat()}


def test_mid_slot_start_uses_opening_bell_not_thirty_minutes_since_selection():
    m=manager();opened=session_bounds(NOW)[0]
    assert select(m,opened+timedelta(minutes=7),100,10)==['A']
    assert m.state['focus_schedule']['next_at']==(opened+timedelta(minutes=30)).isoformat()
    assert select(m,opened+timedelta(minutes=29,seconds=59))==['A']
    assert select(m,opened+timedelta(minutes=30))==['B']
    assert select(m,opened+timedelta(minutes=59),1000,10)==['B']
    assert select(m,opened+timedelta(minutes=60),1000,10)==['A']


@pytest.mark.parametrize('stage',['CORRECTION','REBOUND'])
def test_valid_pattern_keeps_slot_then_becomes_replaceable_after_invalidation(stage):
    m=manager();opened=session_bounds(NOW)[0]
    select(m,opened,100,10)
    boundary=opened+timedelta(minutes=30)
    m.pattern_provider=lambda s,n:pattern(boundary,stage)
    assert select(m,boundary)==['A']
    assert m.state['focus'][0]['pattern_retained']
    m.pattern_provider=lambda s,n:pattern(n,'TREND_MISSING')
    assert select(m,boundary+timedelta(minutes=5))==['A']
    assert select(m,boundary+timedelta(minutes=30))==['B']


def test_boundary_candle_delay_waits_at_most_two_minutes_without_extending_deadline():
    m=manager();opened=session_bounds(NOW)[0]
    select(m,opened,100,10)
    boundary=opened+timedelta(minutes=30)
    old={'stage':'CORRECTION','five_at':(boundary-timedelta(minutes=5)).isoformat(),
        'fifteen_at':(boundary-timedelta(minutes=15)).isoformat()}
    m.pattern_provider=lambda s,n:old
    for seconds in (0,30,119):
        assert select(m,boundary+timedelta(seconds=seconds))==['A']
        assert m.state['focus_schedule']['status']=='BOUNDARY_DATA_WAIT'
        assert m.state['focus_schedule']['pending_until']==(boundary+timedelta(seconds=120)).isoformat()
    assert select(m,boundary+timedelta(seconds=120))==['B']


def test_delayed_boundary_bars_arrive_and_retain_current_valid_pattern():
    m=manager();opened=session_bounds(NOW)[0]
    select(m,opened,100,10)
    boundary=opened+timedelta(minutes=30)
    m.pattern_provider=lambda s,n:{**pattern(boundary),'five_at':(boundary-timedelta(minutes=5)).isoformat()}
    select(m,boundary)
    m.pattern_provider=lambda s,n:pattern(boundary)
    assert select(m,boundary+timedelta(seconds=45))==['A']
    assert m.state['focus_schedule']['status']=='ACTIVE'
    assert m.state['focus'][0]['pattern_retained']


def test_missing_metrics_expire_grace_without_mid_slot_replacement():
    m=manager();opened=session_bounds(NOW)[0]
    select(m,opened,100,10)
    unknown={'ticker':'A','data_status':'UNKNOWN','reason':'price stale'}
    for minutes in (1,2):
        now=opened+timedelta(minutes=minutes)
        choose_scheduled(m,[unknown,metric('B',1000,now)],now)
        assert m.state['focus'][0]['data_status']=='UNKNOWN'
    now=opened+timedelta(minutes=5)
    choose_scheduled(m,[unknown,metric('B',1000,now)],now)
    assert m.state['focus']==[]
    assert select(m,opened+timedelta(minutes=6))==[]
    assert select(m,opened+timedelta(minutes=30))==['B']


def test_empty_preopen_focus_recovers_only_after_fresh_inputs_return():
    from copy import deepcopy
    m=manager();opened=session_bounds(NOW)[0]
    before=opened-timedelta(hours=2)
    assert select(m,before,100,10)==['A']
    # Restore a previously selected slot after an update/long data interruption.
    restarted=manager();restarted.state=deepcopy(m.state)
    later=before+timedelta(minutes=40)
    old=[metric('A',100,before),metric('B',10,before)]
    choose_scheduled(restarted,old,later)
    assert restarted.state['focus']==[]
    choose_scheduled(restarted,old,later+timedelta(seconds=1))
    assert restarted.state['focus']==[]  # Old prices do not become current evidence.
    assert select(restarted,later+timedelta(seconds=2))==['B']
    assert restarted.state['focus_schedule']['status']=='PREOPEN'
    assert restarted.state['focus_schedule']['next_at']==opened.isoformat()
    assert select(restarted,later+timedelta(seconds=3),1000,10)==['B']
    assert select(restarted,opened,1000,10)==['A']


@pytest.mark.parametrize('day,open_hour',[('2026-07-07',13),('2026-11-19',14)])
def test_dst_preopen_and_new_session_reset(day,open_hour):
    m=manager();opened=datetime.fromisoformat(day).replace(hour=open_hour,minute=30,tzinfo=timezone.utc)
    assert session_bounds(opened)[0]==opened
    assert select(m,opened-timedelta(minutes=10),100,10)==['A']
    assert select(m,opened-timedelta(minutes=1))==['A']
    assert m.state['focus_schedule']['next_at']==opened.isoformat()
    assert select(m,opened)==['B']
    assert select(m,opened+timedelta(days=1),100,10)==['A']


def test_holiday_and_early_close_do_not_schedule_after_hours():
    m=manager()
    holiday=datetime(2026,11,26,16,tzinfo=timezone.utc)
    assert select(m,holiday)==[]
    opened,closed=session_bounds(holiday+timedelta(days=1))
    assert closed.hour==18
    select(m,closed-timedelta(minutes=1))
    assert m.state['focus_schedule']['next_at']==closed.isoformat()
    select(m,closed)
    assert m.state['focus_schedule']['status']=='CLOSED'
    assert m.state['focus_schedule']['next_at'] is None


async def test_manager_persists_membership_and_slot_across_restart_and_blocks_stale_entry(tmp_path):
    from sessionquant.watchlist.selection import WatchlistManager
    repo,universe,cfg,provider,_=context(tmp_path,count=60,size=50)
    cfg=replace(cfg,strategy='pullback_simple',focus_selection='turnover',focus_rebalance_minutes=30,focus_size=10)
    m=WatchlistManager(repo,universe,cfg,provider=provider,allow_synthetic=True)
    try:
        await m.refresh(NOW)
        symbols=m.light_symbols(NOW)
        def observe(at,reverse=False):
            m.observe_prices([{'symbol':s,'lastPrice':'100','timestamp':at.isoformat(),'currency':'USD'} for s in symbols],at)
            m.observe_rankings({'rankedAt':at.isoformat(),'rankings':[{'symbol':s,'currency':'USD',
                'tradingAmount':str(i+1 if reverse else 100-i),'tradingVolume':'1000'} for i,s in enumerate(symbols)]},at)
        observe(NOW)
        original=set(m.focus(NOW))
        restarted=WatchlistManager(repo,universe,cfg,provider=provider,allow_synthetic=True)
        m=restarted
        observe(NOW+timedelta(minutes=10),True)
        assert set(m.focus(NOW+timedelta(minutes=10)))==original
        at=NOW+timedelta(minutes=12,seconds=1)
        m.focus(at)
        assert all(m.entry_blockers(s,at) for s in original)
        observe(NOW+timedelta(minutes=30),True)
        assert set(m.focus(NOW+timedelta(minutes=30)))!=original
    finally:repo.close()


async def test_preopen_restart_republishes_focus_before_trading_is_started(tmp_path):
    from sessionquant.watchlist.selection import WatchlistManager, VIEW
    from sessionquant.dashboard.operations_overview import home_focus
    from sessionquant.dashboard.session_app import DashboardData
    repo,universe,cfg,provider,_=context(tmp_path,count=12,size=12)
    before=NOW.replace(hour=13)
    for key in ('as_of_at','known_at','received_at'):
        provider.batch[key]=(before-timedelta(hours=1)).isoformat()
    cfg=replace(cfg,strategy='pullback_simple',focus_selection='turnover',focus_rebalance_minutes=30,focus_size=10)
    m=WatchlistManager(repo,universe,cfg,provider=provider,allow_synthetic=True)
    try:
        await m.refresh(before)
        symbols=m.light_symbols(before)
        def observe(at):
            m.observe_prices([{'symbol':s,'lastPrice':'100','timestamp':at.isoformat(),'currency':'USD'} for s in symbols],at)
            m.observe_rankings({'rankedAt':at.isoformat(),'rankings':[{'symbol':s,'currency':'USD',
                'tradingAmount':str(100-i),'tradingVolume':'1000'} for i,s in enumerate(symbols)]},at)
        observe(before);m.publish(before)
        assert repo.get_control(VIEW)['focus_count']==10
        m=WatchlistManager(repo,universe,cfg,provider=provider,allow_synthetic=True)
        resumed=before+timedelta(minutes=40)
        m.publish(resumed)
        assert repo.get_control(VIEW)['focus_count']==0
        assert all(m.entry_blockers(s,resumed) for s in symbols)
        observe(resumed);m.publish(resumed)
        view=repo.get_control(VIEW)
        assert view['focus_count']==10
        data=DashboardData(watchlist_state=view,engine_state={'mode':'paper','armed':True,
            'running':False,'status':'WAITING','heartbeat':resumed.isoformat()})
        cards=home_focus(data,{'quotes':{}},resumed)
        assert len(cards)==10 and all(c['monitored'] and c['title']=='개장 대기' for c in cards)
        restored=WatchlistManager(repo,universe,cfg,provider=provider,allow_synthetic=True)
        assert set(restored.focus(resumed))==set(m.focus(resumed))
    finally:repo.close()
