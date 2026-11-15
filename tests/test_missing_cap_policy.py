"""Missing market caps narrow the scope; they never certify the full-universe rank."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from sessionquant.market.history import HistoryStore
from sessionquant.market.history_commands import collection_selection
from sessionquant.session_config import SessionConfig
from sessionquant.watchlist.capitalization import WatchlistSelector
from sessionquant.watchlist.history_selection import DailyHistorySelection
from sessionquant.watchlist.selection import WatchlistManager, HistoricalWatchlistPolicy
from test_toss_capitalization import collect
from test_history_selection import prepare
from watchlist_fixtures import context, NOW, observe


def remove(batch, symbols):
    for page in batch['pages'].values():
        page['stocks'] = [r for r in page['stocks'] if r['symbol'] not in symbols]


def manager_for(repo, universe, config, provider, *, history=False):
    return WatchlistManager(repo, universe, replace(config,watchlist_missing_cap_policy='exclude'),
        provider=provider, allow_synthetic=True,
        history_selection=DailyHistorySelection(repo) if history else None)


async def test_missing_seven_does_not_subtract_seven_again_from_known_top_fifty(tmp_path):
    repo, universe, config, _, _ = context(tmp_path,size=50)
    try:
        _, batch, _ = await collect(repo,universe)
        missing={f'T{i:03}' for i in range(7)}
        remove(batch,missing)
        result=WatchlistSelector(50,missing_cap_policy='exclude').select(batch,universe,NOW).payload
        assert result['data_status']=='VALID' and result['coverage_status']=='PARTIAL'
        assert not result['ranking_verified'] and result['ranking_scope']=='available_market_caps'
        assert result['excluded_market_cap_symbols']==sorted(missing)
        assert len(result['items'])==50 and result['valid_observations']==493
        assert [r['ticker'] for r in result['items']]==[f'T{i:03}' for i in range(499,449,-1)]
        assert all(r['data_status']=='VERIFIED' and r['rank_scope']=='available_market_caps' for r in result['items'])
    finally:repo.close()


@pytest.mark.parametrize('value',[None,''])
async def test_missing_value_is_excluded_even_if_another_sector_has_a_value(tmp_path,value):
    repo, universe, config, provider, _ = context(tmp_path)
    try:
        _, batch, _ = await collect(repo,universe)
        batch['pages']['b']['stocks'][-1]['marketCapitalization']=value
        result=WatchlistSelector(50,missing_cap_policy='exclude').select(batch,universe,NOW).payload
        assert result['data_status']=='VALID' and result['excluded_market_cap_symbols']==['T499']
        assert result['items'][0]['ticker']=='T498'
        assert result['market_cap_exclusions'][0]['reasons']==['market_cap_value_missing']
        manager=manager_for(repo,universe,replace(config,watchlist_size=50),provider)
        manager.store.add(result)
        from sessionquant.watchlist.diagnostics import diagnose
        diagnosis=await diagnose(repo,manager,now=NOW)
        assert diagnosis['missing_market_cap_symbols']==['T499'] and not diagnosis['ranking_verified']
        _,selection=collection_selection(manager,NOW)
        assert selection['missing_market_cap_symbols']==['T499']
    finally:repo.close()


@pytest.mark.parametrize('change',['conflict','currency','duplicate','negative','nan','boolean','reference'])
async def test_exclusion_policy_does_not_hide_other_data_errors(tmp_path,change):
    repo, universe, _, _, _ = context(tmp_path)
    try:
        _, batch, _ = await collect(repo,universe)
        if change=='conflict':batch['pages']['b']['stocks'][-1]['marketCapitalization']='99'
        elif change=='currency':batch['pages']['b']['stocks'][-1]['currency']='USD'
        elif change=='duplicate':batch['pages']['b']['stocks'].append(deepcopy(batch['pages']['b']['stocks'][-1]))
        elif change=='reference':
            from sessionquant.watchlist.capitalization import symbols_digest
            universe.refresh([*universe.members(NOW),'SPY'],source='SYNTHETIC_FIXTURE',observed_at=NOW)
            batch['membership_hash']=symbols_digest(universe.members(NOW))
            batch['pages']['a']['stocks'].append({'symbol':'SPY','currency':'KRW','marketCapitalization':'999999999'})
        else:batch['pages']['b']['stocks'][-1]['marketCapitalization']={'negative':'-1','nan':'NaN','boolean':True}[change]
        result=WatchlistSelector(50,missing_cap_policy='exclude').select(batch,universe,NOW).payload
        assert result['data_status']=='PARTIAL' and not result['ranking_verified']
    finally:repo.close()


async def test_fewer_than_limit_is_valid_without_refresh_loop_and_backfills_all_base_members(tmp_path):
    repo, universe, config, provider, _ = context(tmp_path,size=50)
    try:
        toss, batch, _ = await collect(repo,universe)
        keep={'T497','T498','T499'}
        remove(batch,set(universe.members(NOW))-keep)
        class Provider:
            calls=0
            async def fetch(self):
                self.calls+=1
                return batch
        source=Provider()
        manager=manager_for(repo,universe,config,source)
        first=await manager.refresh(NOW)
        assert first['status']=='VALID' and len(first['items'])==3
        await manager.refresh(NOW+timedelta(seconds=1))
        assert source.calls==1
        symbols, selection=collection_selection(manager,NOW)
        assert set(symbols)==keep and not selection['collection_only']
        assert not selection['ranking_verified'] and selection['ranking_scope']=='available_market_caps'
        assert len(selection['excluded_market_cap_symbols'])==497
        assert manager.store.status(NOW+timedelta(days=1))['status']=='EXPIRED'
        remove(batch,keep)
        empty=WatchlistSelector(50,missing_cap_policy='exclude').select(batch,universe,NOW).payload
        assert empty['data_status']=='PARTIAL' and empty['items']==[]
    finally:repo.close()


async def test_cached_partial_reselected_on_policy_change_then_history_filter_and_replay(tmp_path):
    repo, universe, config, provider, old = context(tmp_path,size=50)
    try:
        provider.batch['observations']=provider.batch['observations'][7:]
        before=NOW-timedelta(seconds=1)
        previous=await old.refresh(before)
        assert previous['status']=='PARTIAL'
        original=deepcopy(old.store.at(before))
        prepare(HistoryStore(repo),'T499',known=NOW)
        manager=manager_for(repo,universe,config,provider,history=True)
        after=NOW
        result=await manager.refresh(after)
        assert provider.calls==2 and result['status']=='VALID'
        assert manager.store.at(before)==original
        stamp=observe(manager)
        assert manager.light_symbols(stamp)==['T499'] and manager.focus(stamp)==['T499']
        assert manager.entry_blockers('T499',stamp)==[]
        assert manager.entry_blockers('T000',stamp)==['outside_market_cap_watchlist']
        manager.publish(stamp)
        frames=[r for r in repo.all('pipeline_status') if r.get('kind')=='watchlist_focus']
        policy=HistoricalWatchlistPolicy({'schema_version':1,'snapshots':repo.get_control('watchlist_snapshots_v1'),
                                         'focus_history':frames},universe,allow_synthetic=True)
        assert policy.entry_blockers('T499',stamp)==[]
        assert policy.entry_blockers('T000',stamp)==['outside_market_cap_watchlist']
        strict=WatchlistManager(repo,universe,config,provider=provider,allow_synthetic=True)
        assert strict.store.status(stamp,allow_synthetic=True)['status']=='PARTIAL'
        assert strict.entry_blockers('T499',stamp)==['watchlist_partial']
    finally:repo.close()


async def test_excluded_cap_held_symbol_still_receives_priority_quotes(tmp_path):
    from sessionquant.market.source import TossSessionSource
    from test_watchlist_source import Market
    repo, universe, config, provider, _ = context(tmp_path,size=50)
    try:
        provider.batch['observations']=provider.batch['observations'][:-1]
        manager=manager_for(repo,universe,config,provider,history=True)
        prepare(HistoryStore(repo),'T498')
        await manager.refresh(NOW)
        observe(manager)
        stamp=NOW+timedelta(seconds=60)
        client=Market(lambda:stamp)
        source=TossSessionSource(client,watchlist=manager,clock=lambda:stamp)
        await source.poll([],priority_symbols=['T499'])
        assert [v for k,v in client.calls if k=='quote'][0]=='T499'
        assert next(v for k,v in client.calls if k=='prices')==['T498','SPY','QQQ']
        assert 'T499' not in manager.light_symbols(stamp)
        assert repo.all('orders')==[]
    finally:repo.close()


def test_default_policy_is_exclude_and_block_remains_explicit():
    assert SessionConfig(company_rss_catalog=False).watchlist_missing_cap_policy=='exclude'
    assert SessionConfig(company_rss_catalog=False,watchlist_missing_cap_policy='block').watchlist_missing_cap_policy=='block'
    with pytest.raises(ValueError):SessionConfig(company_rss_catalog=False,watchlist_missing_cap_policy='ignore_all')


async def test_file_missing_value_keeps_issuer_validation(tmp_path):
    repo, universe, _, provider, _ = context(tmp_path,size=50)
    try:
        provider.batch['observations'][-1]['market_cap_usd']=None
        selector=WatchlistSelector(50,missing_cap_policy='exclude')
        result=selector.select(provider.batch,universe,NOW,allow_synthetic=True).payload
        assert result['data_status']=='VALID' and result['excluded_market_cap_symbols']==['T499']
        provider.batch['observations'][-1]['issuer_verified']=False
        assert selector.select(provider.batch,universe,NOW,allow_synthetic=True).payload['data_status']=='PARTIAL'
    finally:repo.close()


def test_dashboard_does_not_show_subset_rank_as_full_universe_rank():
    from datetime import datetime,timezone
    from streamlit.testing.v1 import AppTest
    now=datetime.now(timezone.utc).isoformat()
    state={'status':'VALID','updated_at':now,'watchlist_limit':50,'operating_count':1,
           'snapshot':{'as_of_at':now,'ranking_verified':False,'excluded_market_cap_symbols':['MISSING']},
           'items':[{'ticker':'AAPL','rank':1}]}
    app=AppTest.from_string('from sessionquant.dashboard.watchlist_panel import render_watchlist\n'
                            f'render_watchlist({state!r},[])').run()
    assert not app.exception
    assert any(m.label=='유효 운영 종목' and m.value=='1 / 50' for m in app.metric)
    assert any('MISSING' in row.value and '전체 S&P 500 상위 순위는 미확정' in row.value for row in app.info)
    table=app.dataframe[0].value
    assert table['시총 순위'].isna().all() and table['검증자료 내 순위'][0]==1


async def test_policy_migration_does_not_repeatedly_bypass_retry_after(tmp_path):
    import httpx
    repo, universe, config, provider, old=context(tmp_path,size=50)
    try:
        provider.batch['observations']=provider.batch['observations'][1:]
        await old.refresh(NOW)
        class Limited:
            calls=0
            async def fetch(self):
                self.calls+=1
                httpx.Response(429,headers={'Retry-After':'120'},request=httpx.Request('GET','https://fixture.invalid')).raise_for_status()
        limited=Limited()
        manager=manager_for(repo,universe,config,limited)
        first=await manager.refresh(NOW+timedelta(seconds=1))
        assert first['status']=='PARTIAL' and first['refresh']['http_status']==429
        await manager.refresh(NOW+timedelta(seconds=2))
        assert limited.calls==1
        await manager.refresh(NOW+timedelta(seconds=122))
        assert limited.calls==2
    finally:repo.close()
