"""시총 정합성·시점 이력·집중 후보·재시작 공정성·뉴스 대상 경계를 검증한다."""
from copy import deepcopy
from datetime import timedelta
from random import Random

import httpx
import pytest

from sessionquant.watchlist.capitalization import WatchlistSelector, WatchlistStore
from sessionquant.watchlist.selection import FairScheduler, HistoricalWatchlistPolicy
from watchlist_fixtures import NOW, cap_batch, context, observe


@pytest.mark.parametrize('base',['95',None,'0','NaN',True])
async def test_ranking_previous_day_base_reaches_cards_without_changing_focus(tmp_path,base):
    from sessionquant.dashboard.home_cards import focus_cards
    from sessionquant.watchlist.selection import VIEW
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager)
        symbols=manager.light_symbols(now)
        rows=[{'symbol':symbol,'currency':'USD','tradingAmount':'10000000','tradingVolume':'10000',
               'price':{'lastPrice':'99','basePrice':base,'changeRate':'-0.90'}} for symbol in symbols]
        manager.observe_rankings({'rankedAt':now.isoformat(),'rankings':rows},now)
        manager.publish(now)
        view=repo.get_control(VIEW)
        cards=focus_cards(view,now=now)
        assert len(cards)==10  # Main dashboard shows the first ten focused symbols.
        assert cards[0]['price']=='$101.00'
        assert cards[0]['change']==('+6.31%' if base=='95' else '—')
        assert len(manager.state['focus'])==20
        # Display data never turns into strategy/order evidence or synthetic prices.
        assert repo.all('orders')==[]
        assert all(r['last_price']=='101' for r in view['items'])
    finally:repo.close()


def test_capitalization_order_is_not_alphabetical_and_ties_are_deterministic(tmp_path):
    repo,universe,_,provider,manager=context(tmp_path)
    try:
        Random(7).shuffle(provider.batch['observations'])
        result=WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
        assert result.symbols==[f'T{i:03}' for i in range(499,399,-1)]
        assert len(universe.members(NOW))==500
        provider.batch['observations'][0]['market_cap_usd']='500000000'
        one=WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
        Random(11).shuffle(provider.batch['observations'])
        two=WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
        assert one.payload==two.payload
        assert one.payload['items'][0]['rank']==1
    finally:repo.close()


@pytest.mark.parametrize('value',[None,'-1','0','NaN','Infinity',True])
def test_bad_market_caps_are_unknown_and_never_certify_top100(tmp_path,value):
    repo,universe,_,provider,_=context(tmp_path)
    try:
        provider.batch['observations'][-1]['market_cap_usd']=value
        result=WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
        assert result.payload['data_status']=='PARTIAL'
        assert 'T499' not in result.symbols
        assert any(r['ticker']=='T499' for r in result.payload['issues'])
    finally:repo.close()


@pytest.mark.parametrize(('field','value'),[
 ('currency','KRW'),('source_verified',False),('market_cap_basis','security_class'),('as_of_at',None),
 ('as_of_at','2027-01-01T00:00:00+00:00'),('known_at','2027-01-01T00:00:00+00:00'),
 ('received_at','2026-01-01T00:00:00+00:00'),('as_of_at','2026-01-01T00:00:00+00:00'),
 ('observations',[])])
def test_unsupported_missing_future_or_stale_batch_is_rejected(tmp_path,field,value):
    repo,universe,_,provider,_=context(tmp_path)
    try:
        provider.batch[field]=value
        with pytest.raises((ValueError,TypeError,KeyError)):
            WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
    finally:repo.close()


def test_omission_duplicate_ticker_and_row_currency_are_not_silently_accepted(tmp_path):
    repo,universe,_,provider,_=context(tmp_path)
    try:
        missing=deepcopy(provider.batch);missing['observations'].pop()
        assert WatchlistSelector().select(missing,universe,NOW,allow_synthetic=True).payload['data_status']=='PARTIAL'
        duplicate=deepcopy(provider.batch);duplicate['observations'].append(duplicate['observations'][0])
        with pytest.raises(ValueError,match='중복'):
            WatchlistSelector().select(duplicate,universe,NOW,allow_synthetic=True)
        conflict=deepcopy(provider.batch);conflict['observations'][0]['currency']='CAD'
        assert WatchlistSelector().select(conflict,universe,NOW,allow_synthetic=True).payload['data_status']=='PARTIAL'
        conflict=deepcopy(provider.batch);conflict['observations'][0]['known_at']='2027-01-01T00:00:00+00:00'
        assert WatchlistSelector().select(conflict,universe,NOW,allow_synthetic=True).payload['data_status']=='PARTIAL'
    finally:repo.close()


def test_multiple_share_classes_require_equal_issuer_total_and_representative_evidence(tmp_path):
    repo,universe,_,provider,_=context(tmp_path,count=5,size=5)
    try:
        rows=provider.batch['observations']
        rows[-2]['issuer_id']=rows[-1]['issuer_id']
        rows[-2]['market_cap_usd']=rows[-1]['market_cap_usd']
        selector=WatchlistSelector(size=5)
        unknown=selector.select(provider.batch,universe,NOW,allow_synthetic=True)
        assert unknown.payload['data_status']=='PARTIAL'
        assert not {'T003','T004'}&set(unknown.symbols)
        for i in (3,4):
            rows[i].update(data_supported=True,representative_dollar_volume_usd=str(i*1000),
                representative_as_of_at=provider.batch['as_of_at'],representative_evidence_source='SYNTHETIC_FIXTURE')
        valid=selector.select(provider.batch,universe,NOW,allow_synthetic=True)
        assert valid.payload['data_status']=='VALID'
        assert valid.symbols==['T004','T002','T001','T000']
        assert valid.payload['items'][0]['market_cap_usd']=='5000000'
        rows[-2]['market_cap_usd']='6000000'
        assert selector.select(provider.batch,universe,NOW,allow_synthetic=True).payload['data_status']=='PARTIAL'
    finally:repo.close()


async def test_snapshots_do_not_backdate_and_staleness_is_separate_from_membership(tmp_path):
    repo,universe,_,provider,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        assert manager.store.at(NOW-timedelta(seconds=1)) is None
        assert manager.store.status(NOW)['status']=='SYNTHETIC_BLOCKED'
        assert manager.store.status(NOW,allow_synthetic=True)['status']=='VALID'
        assert len(WatchlistStore(repo,universe).at(NOW)['items'])==100
        universe.refresh(list(universe.members(NOW)),source='SYNTHETIC_FIXTURE',observed_at=NOW+timedelta(days=8))
        assert manager.store.status(NOW+timedelta(days=8),allow_synthetic=True)['status']=='EXPIRED'
        universe.refresh(['OTHER'],source='SYNTHETIC_FIXTURE',observed_at=NOW+timedelta(days=8,seconds=1))
        assert manager.store.status(NOW+timedelta(days=8,seconds=1),allow_synthetic=True)['status']=='MEMBERSHIP_CHANGED'
    finally:repo.close()


@pytest.mark.parametrize('status',[403,429])
async def test_provider_http_failure_keeps_original_snapshot_and_redacts_secrets(tmp_path,status):
    repo,universe,_,provider,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        original=manager.store.at(NOW)
        async def broken():
            response=httpx.Response(status,headers={'Retry-After':'120'},request=httpx.Request('GET','https://provider.test?apikey=PRIVATE'))
            response.raise_for_status()
        provider.fetch=broken
        result=await manager.refresh(NOW+timedelta(seconds=1),force=True)
        assert manager.store.at(NOW+timedelta(seconds=1))==original
        assert result['refresh']['http_status']==status
        assert 'PRIVATE' not in str(result)
        assert result['status']=='VALID'
        assert result['snapshot']['known_at']==NOW.isoformat()
    finally:repo.close()


async def test_weekly_refresh_does_not_refresh_knowledge_when_no_new_data(tmp_path):
    repo,_,_,provider,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        await manager.refresh(NOW+timedelta(days=1))
        assert provider.calls==1
        assert manager.store.at(NOW+timedelta(days=1))['known_at']==NOW.isoformat()
        provider.batch['observations']=[]
        await manager.refresh(NOW+timedelta(days=1),force=True)
        assert manager.store.at(NOW+timedelta(days=1))['known_at']==NOW.isoformat()
    finally:repo.close()


async def test_focus_uses_observed_activity_not_top20_caps_and_keeps_permissions_unknown(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        assert manager.focus(NOW)==[]
        now=observe(manager)
        focused=manager.focus(now)
        assert len(focused)==20
        assert set(focused)=={f'T{i:03}' for i in range(400,420)}
        assert not set(focused)&{f'T{i:03}' for i in range(480,500)}
        assert all(r['rvol'] is None and r['tradable'] is None and r['fractional'] is None for r in manager.state['focus'])
        assert manager.entry_blockers(focused[0],now)==[]
        assert manager.entry_blockers('T000',now)==['outside_market_cap_watchlist']
        assert set(manager.focus(now+timedelta(seconds=121)))==set(focused)
        assert manager.entry_blockers(focused[0],now+timedelta(seconds=121))==['focus_refresh_pending']
        manager.publish(now+timedelta(seconds=121))
        assert repo.get_control('watchlist_dashboard_v1')['focus_count']==20
        assert manager.focus(now+timedelta(seconds=241))==[]
        manager.publish(now+timedelta(seconds=241))
        assert repo.get_control('watchlist_dashboard_v1')['focus_count']==0
    finally:repo.close()


async def test_focus_hysteresis_keeps_valid_incumbents_and_news_risk_bypasses_caps(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager)
        focused=manager.focus(now)
        for s in manager.state['rankings']:manager.state['rankings'][s]['dollar_volume_usd']='1'
        manager.state['rankings']['T499']['dollar_volume_usd']='999999999999'
        assert manager.focus(now+timedelta(seconds=1))==focused
        contract=manager.news_targets(now,['OUTSIDE','T000'])
        assert contract['risk_priority']==['OUTSIDE','T000']
        assert len(contract['company_allowlist'])==100 and contract['macro_independent']
        assert contract['analysis_symbols'][:2]==['OUTSIDE','T000']
    finally:repo.close()


def test_fair_rotation_survives_restart_and_failed_ticker_does_not_dominate():
    state={};visited=[]
    names=[f'T{i:03}' for i in range(20)]
    for cycle in range(10):
        scheduler=FairScheduler(state)
        chosen=scheduler.choose(names,2);visited+=chosen
        for s in chosen:scheduler.mark(s,NOW+timedelta(seconds=cycle))
        state=deepcopy(state)
    assert len(set(visited))==20
    assert FairScheduler(state).choose(names,2)==names[:2]


async def test_historical_policy_does_not_use_future_focus_or_current_rss_list(tmp_path):
    repo,universe,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager);manager.publish(now)
        history={'schema_version':1,'snapshots':repo.get_control('watchlist_snapshots_v1'),
            'focus_history':repo.all('pipeline_status')}
        policy=HistoricalWatchlistPolicy(history,universe,allow_synthetic=True)
        assert policy.entry_blockers('T400',NOW)==['focus_history_unknown_or_stale']
        assert policy.entry_blockers('T400',now)==[]
        assert policy.entry_blockers('T400',now+timedelta(minutes=5))==['focus_history_unknown_or_stale']
        assert policy.entry_blockers('T400',NOW-timedelta(seconds=1))==['watchlist_history_unknown']
    finally:repo.close()


async def test_missing_timestamp_and_unknown_issuer_do_not_crash_refresh(tmp_path):
    repo,_,_,provider,manager=context(tmp_path)
    try:
        provider.batch['as_of_at']=None
        status=await manager.refresh(NOW)
        assert status['status']=='UNKNOWN' and manager.light_symbols(NOW)==[]
        provider.batch=cap_batch();provider.batch['observations'][0]['issuer_verified']=False
        status=await manager.refresh(NOW,force=True)
        assert status['status']=='PARTIAL' and manager.light_symbols(NOW)==[]
    finally:repo.close()


def test_source_metadata_cannot_echo_api_credentials(tmp_path):
    repo,universe,_,provider,_=context(tmp_path)
    try:
        provider.batch['source_documentation']='https://provider.test/docs?apikey=PRIVATE'
        with pytest.raises(ValueError,match='인증정보'):
            WatchlistSelector().select(provider.batch,universe,NOW,allow_synthetic=True)
    finally:repo.close()


async def test_empty_closed_market_ranking_remains_unknown_without_fake_volume(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        now=observe(manager)
        assert len(manager.focus(now))==20
        manager.observe_rankings({'rankedAt':None,'rankings':[]},now)
        assert len(manager.focus(now))==20 and manager.state['rankings']=={}
        assert all(r['data_status']=='UNKNOWN' for r in manager.state['focus'])
        assert manager.entry_blockers('T400',now)==['focus_refresh_pending']
        assert manager.focus(now+timedelta(seconds=121))==[]
    finally:repo.close()


async def test_slow_provider_times_out_without_replacing_known_snapshot(tmp_path, monkeypatch):
    """공급자 지연을 제한하고 원래 선택 시각·시총을 그대로 보존한다."""
    import asyncio
    repo, _, _, provider, manager = context(tmp_path)
    try:
        await manager.refresh(NOW)
        original = manager.store.at(NOW)
        wait_for = asyncio.wait_for

        async def slow_fetch():
            await asyncio.Event().wait()

        async def short_timeout(awaitable, *, timeout):
            assert timeout == 5
            return await wait_for(awaitable, timeout=0.01)

        provider.fetch = slow_fetch
        monkeypatch.setattr('sessionquant.watchlist.selection.asyncio.wait_for', short_timeout)
        result = await manager.refresh(NOW+timedelta(seconds=1), force=True)
        assert result['refresh']['error_type'] == 'TimeoutError'
        assert result['snapshot'] == original and result['status'] == 'VALID'
        assert result['snapshot']['known_at'] == NOW.isoformat()
    finally:
        repo.close()


async def test_focus_diagnostics_distinguish_missing_inputs_and_keep_quality_gates(tmp_path):
    repo,_,_,_,manager=context(tmp_path)
    try:
        await manager.refresh(NOW)
        manager.publish(NOW)
        row=next(r for r in repo.get_control('watchlist_dashboard_v1')['items'] if r['ticker']=='T400')
        assert set(row['focus_missing_inputs'])=={'symbol_prices_missing','spy_prices_missing','turnover_missing'}
        now=observe(manager)
        original=deepcopy(manager.state)
        manager.state['prices']['T400'].pop()
        assert manager._metric('T400',now)['missing_inputs']==['symbol_second_price_missing']
        manager.state=deepcopy(original)
        manager.state['rankings'].pop('T400')
        assert manager._metric('T400',now)['missing_inputs']==['turnover_missing']
        assert 'T400' not in manager.focus(now)
        manager.state=deepcopy(original)
        assert manager._metric('T400',now+timedelta(seconds=121))['missing_inputs']==['observation_stale_or_future']
        manager.state['prices']['T400'][0]['known_at']=(now-timedelta(seconds=2)).isoformat()
        assert manager._metric('T400',now)['missing_inputs']==['observation_window_mismatch']
        assert 'T400' not in manager.focus(now)
    finally:repo.close()
