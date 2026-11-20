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
