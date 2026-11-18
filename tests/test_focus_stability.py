"""Keep collection targets stable without carrying stale evidence into trading."""
from copy import deepcopy
from datetime import timedelta

import pytest

from sessionquant.dashboard.watchlist_panel import focus_is_monitored
from sessionquant.watchlist.selection import FocusSelector, HistoricalWatchlistPolicy, WatchlistManager
from watchlist_fixtures import NOW, context, observe


async def test_refresh_wait_survives_restart_but_never_extends_itself_or_allows_entry(tmp_path):
    repo,universe,config,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager)
        original=set(manager.focus(now))
        manager.state['prices']['T400'][0]['known_at']=(now-timedelta(seconds=2)).isoformat()
        waiting=now+timedelta(seconds=1)
        assert set(manager.focus(waiting))==original
        assert manager.entry_blockers('T400',waiting)==['focus_refresh_pending']
        assert manager.candidate_expiry('T400',waiting)==waiting.isoformat()
        manager.publish(waiting,priority_symbols=['OUTSIDE'])
        view=repo.get_control('watchlist_dashboard_v1')
        row=next(r for r in view['items'] if r['ticker']=='T400')
        assert row['focused'] and row['focus_refresh_pending'] and focus_is_monitored(row,waiting)
        assert view['news_contract']['risk_priority']==['OUTSIDE']
        saved=next(r for r in manager.state['focus'] if r['ticker']=='T400')
        deadline=saved['refresh_wait_until']
        policy=HistoricalWatchlistPolicy({'schema_version':1,'snapshots':repo.get_control('watchlist_snapshots_v1'),
            'focus_history':repo.all('pipeline_status')},universe,allow_synthetic=True)
        assert policy.entry_blockers('T400',waiting)==['focus_history_unknown_or_stale']
        assert policy.entry_blockers('T401',waiting)==[]
        manager=WatchlistManager(repo,universe,config,allow_synthetic=True)
        for offset in [10,30,60]:
            at=now+timedelta(seconds=offset)
            assert 'T400' in manager.focus(at)
            assert next(r for r in manager.state['focus'] if r['ticker']=='T400')['refresh_wait_until']==deadline
            assert manager.entry_blockers('T400',at)==['focus_refresh_pending']
        assert 'T400' not in manager.focus(now+timedelta(seconds=122))
        assert not focus_is_monitored(row,now+timedelta(seconds=122))
    finally:repo.close()


async def test_aligned_data_resumes_existing_focus_and_scope_removal_is_immediate(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager)
        manager.focus(now)
        original=deepcopy(manager.state['prices']['T400'])
        manager.state['prices']['T400'][0]['known_at']=(now-timedelta(seconds=2)).isoformat()
        manager.focus(now+timedelta(seconds=1))
        assert manager.entry_blockers('T400',now+timedelta(seconds=1))
        manager.state['prices']['T400']=original
        assert 'T400' in manager.focus(now+timedelta(seconds=2))
        row=next(r for r in manager.state['focus'] if r['ticker']=='T400')
        assert row['focus_since']==now.isoformat() and not row.get('refresh_wait_until')
        assert manager.entry_blockers('T400',now+timedelta(seconds=2))==[]
        allowed=manager.light_symbols(now)
        manager.light_symbols=lambda _: [s for s in allowed if s!='T400']
        assert 'T400' not in manager.focus(now+timedelta(seconds=3))
    finally:repo.close()


def metric(symbol,amount,now):
    return {'ticker':symbol,'data_status':'VERIFIED','known_at':now.isoformat(),
        'valid_until':(now+timedelta(seconds=120)).isoformat(),'dollar_volume_usd':str(amount),
        'relative_strength_sample':'0','sample_return':'0'}


def test_score_change_waits_five_minutes_without_resetting_original_membership_time():
    selector=FocusSelector(size=1)
    chosen=selector.choose([metric('A',100,NOW),metric('B',10,NOW)],[],NOW)
    for offset in [1,60,299]:
        at=NOW+timedelta(seconds=offset)
        chosen=selector.choose([metric('A',10,at),metric('B',100,at)],chosen,at)
        assert chosen[0]['ticker']=='A' and chosen[0]['focus_since']==NOW.isoformat()
    at=NOW+timedelta(seconds=300)
    assert selector.choose([metric('A',10,at),metric('B',100,at)],chosen,at)[0]['ticker']=='B'


@pytest.mark.parametrize('seconds', [0,1,120])
def test_refresh_wait_never_selects_a_new_unknown_symbol(seconds):
    selector=FocusSelector(size=1,refresh_grace_seconds=seconds)
    unknown={'ticker':'A','data_status':'UNKNOWN','reason':'missing'}
    assert selector.choose([unknown],[],NOW)==[]
    selected=selector.choose([metric('A',100,NOW)],[],NOW)
    at=NOW+timedelta(seconds=1)
    result=selector.choose([unknown],selected,at)
    assert bool(result)==bool(seconds)
    if result:
        assert result[0]['data_status']=='UNKNOWN'
        assert result[0]['valid_until']==selected[0]['valid_until']
