"""Synthetic US listings: fixed daily candidates, complete catalog and risk subscriptions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from sessionquant.collectors.toss_market import TossMarketClient
from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.session_config import SessionConfig
from sessionquant.watchlist.selection import WatchlistManager
from sessionquant.watchlist.toss_capitalization import TossMarketCapProvider, CollectionPending
from sessionquant.watchlist.us_universe import MARKETS, SOURCE, POLICY, TossUSUniverse, catalog_members

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def stock(symbol, kind='STOCK', common=True):
    return {'symbol':symbol, 'name':'합성 '+symbol, 'securityType':kind, 'isCommonShare':common, 'isinCode':'fixture-'+symbol}


@pytest.fixture
def setup(tmp_path):
    repo = Repository(tmp_path/'paper.db', backend='sqlite')
    universe = UniverseStore(tmp_path/'us.json')
    clock = SimpleNamespace(now=NOW)
    config = SessionConfig(database_backend='sqlite', db_path=repo.path, universe_path=universe.path,
        universe_source='toss_us', market_cap_provider='toss', watchlist_refresh_days=1, company_rss_catalog=False,
        strategy='pullback_simple', focus_selection='turnover', focus_rebalance_minutes=30, focus_size=10)
    provider = TossMarketCapProvider(repo, universe, clock=lambda:clock.now, daily_us=True)
    manager = WatchlistManager(repo, universe, config, provider=provider, clock=lambda:clock.now)
    listings = {market:[stock(f'T{i:03}') for i in range(index,51,3)] for index,market in enumerate(MARKETS)}
    listings['NASDAQ'] += [stock('IPO'), stock('SPY','ETF'), stock('PREF',common=False)]
    caps = {f'T{i:03}':str(1000-i) for i in range(51)}
    caps['IPO'] = None
    calls = []
    async def read(stream, symbol):
        calls.append((stream, symbol))
        if stream == 'stocks_all':return deepcopy(listings[symbol])
        if stream == 'sectors':return {'sectors':[{'sectorId':'all','depth':0}]}
        assert stream == 'sector_stocks' and symbol == 'all'
        return {'stocks':[{'symbol':s,'name':'합성 '+s,'marketCapitalization':v,'currency':'KRW'} for s,v in caps.items()]}
    value = SimpleNamespace(repo=repo, universe=universe, clock=clock, config=config,
                            provider=provider, manager=manager, listings=listings, caps=caps, calls=calls, read=read)
    yield value
    repo.close()


async def collect(value):
    for _ in range(12):
        if await value.provider.advance(value.read):
            return await value.manager.refresh(value.clock.now, force=True)
    pytest.fail('collection did not finish')


async def test_daily_top50_freezes_rank_flip_restart_and_updates_next_trading_day(setup):
    s = setup
    result = await collect(s)
    assert result['status'] == 'VALID'
    assert s.calls == [('stocks_all',m) for m in MARKETS] + [('sectors',None),('sector_stocks','all')]
    first = deepcopy(result['snapshot'])
    assert first['selection_policy'] == POLICY and first['selection_phase'] == 'PREOPEN'
    assert first['valid_until'] == '2026-11-19T21:00:00+00:00'
    assert len(s.manager.base_symbols(s.clock.now)) == 50
    assert 'T049' in s.manager.base_symbols(s.clock.now) and 'T050' not in s.manager.base_symbols(s.clock.now)
    assert 'IPO' in first['excluded_market_cap_symbols'] and not first['ranking_verified']
    assert 'SPY' not in s.universe.members(s.clock.now) and 'PREF' not in s.universe.members(s.clock.now)
    assert s.universe.snapshot(s.clock.now)['source'] == SOURCE
    assert s.universe.snapshot(NOW-timedelta(seconds=1)) is None

    # Even a force-refresh and raw cap change do not reselect during this session.
    s.clock.now = NOW.replace(hour=17)
    s.caps['T050'] = '3000'
    s.provider.state['pages']['all']['stocks'][-2]['marketCapitalization'] = '3000'
    assert await s.provider.advance(s.read)
    assert (await s.manager.refresh(s.clock.now, force=True))['snapshot'] == first
    assert len(s.calls) == 5
    s.provider = TossMarketCapProvider(s.repo, UniverseStore(s.universe.path), clock=lambda:s.clock.now, daily_us=True)
    s.universe = s.provider.universe
    s.manager = WatchlistManager(s.repo, s.universe, s.config, provider=s.provider, clock=lambda:s.clock.now)
    assert (await collect(s))['snapshot'] == first
    assert len(s.calls) == 5

    s.clock.now = NOW+timedelta(days=1)
    assert s.manager.base_symbols(s.clock.now) == []  # Yesterday's membership cannot grant today's entries.
    result = await collect(s)
    assert len(s.calls) == 10 and result['snapshot']['session_date'] == '2026-11-20'
    assert 'T050' in s.manager.base_symbols(s.clock.now) and 'T049' not in s.manager.base_symbols(s.clock.now)
    assert not s.repo.all('orders') and not s.repo.all('executions')


async def test_incomplete_exchange_never_publishes_partial_universe_or_falls_back_to_sp500(setup):
    s = setup
    s.universe.refresh(['OLD'],source='S&P fixture',observed_at=NOW-timedelta(days=1))
    original = deepcopy(s.universe.snapshots)
    async def interrupted(stream, symbol):
        if symbol == 'AMEX':return None
        return await s.read(stream, symbol)
    for _ in range(4):assert not await s.provider.advance(interrupted)
    assert s.universe.snapshots == original and not s.manager.base_symbols(s.clock.now)
    with pytest.raises(CollectionPending):await s.provider.fetch()
    assert not s.repo.all('orders')
    assert (await collect(s))['status'] == 'VALID'
    assert 'OLD' not in s.universe.members(s.clock.now)


async def test_intraday_first_start_ipo_missing_caps_and_next_day_availability(setup):
    s=setup
    s.clock.now=NOW.replace(hour=16)
    result=await collect(s)
    assert result['snapshot']['selection_phase']=='INTRADAY_START'
    assert 'IPO' not in s.manager.light_symbols(s.clock.now)
    s.caps['IPO']='9999'
    assert (await s.manager.refresh(s.clock.now,force=True))['snapshot']['id']==result['snapshot']['id']
    s.clock.now=NOW+timedelta(days=1)
    result=await collect(s)
    assert result['items'][0]['ticker']=='IPO'
    assert result['snapshot']['ranking_verified']


async def test_same_day_missing_local_cache_is_rehydrated_without_new_listing_requests(setup):
    s=setup
    result=await collect(s)
    s.universe.path.unlink()
    catalog=TossUSUniverse(s.repo,UniverseStore(s.universe.path),clock=lambda:s.clock.now)
    assert not catalog.ready(s.clock.now)
    assert await catalog.advance(s.read)
    assert len(s.calls)==5
    assert catalog.universe.snapshot(s.clock.now)['known_at']==result['snapshot']['membership_known_at']


@pytest.mark.parametrize('fault',['empty_exchange','duplicate','identity_conflict','unknown_common','bad_symbol'])
def test_catalog_rejects_incomplete_or_ambiguous_listing_data(fault):
    pages={m:{'stocks':[stock('S'+str(i))]} for i,m in enumerate(MARKETS)}
    if fault=='empty_exchange':pages['NYSE']['stocks']=[]
    elif fault=='duplicate':pages['NYSE']['stocks']*=2
    elif fault=='identity_conflict':
        pages['NYSE']['stocks']=[stock('S1')]
        pages['NASDAQ']['stocks'][0]['isinCode']='different'
    elif fault=='unknown_common':pages['NYSE']['stocks'][0]['isCommonShare']=None
    elif fault=='bad_symbol':pages['NYSE']['stocks'][0]['symbol']='../orders'
    with pytest.raises(ValueError):catalog_members(pages)


def test_catalog_includes_adr_and_reit_but_excludes_funds_and_preferred():
    pages={m:{'stocks':[stock('S'+str(i))]} for i,m in enumerate(MARKETS)}
    pages['NYSE']['stocks'] += [stock('ADR','DEPOSITARY_RECEIPT'),stock('REIT','REIT'),
                               stock('ETF','FOREIGN_ETF'),stock('ETN','ETN'),stock('WARRANT','STOCK_WARRANTS')]
    assert {r['symbol'] for r in catalog_members(pages)} == {'S0','S1','S2','ADR','REIT'}


async def test_new_day_subscriptions_keep_held_and_pending_symbols_and_discard_old_generation(setup):
    from sessionquant.collectors.toss_quote_feed import TossQuoteFeed
    from sessionquant.market.live_quotes import LiveQuoteDelivery
    s=setup
    await collect(s)
    feed=TossQuoteFeed(object(),clock=lambda:s.clock.now)
    delivery=LiveQuoteDelivery(feed)
    broker=SimpleNamespace(positions={'T049':{}},exit_intents={},
                           get_open_orders=lambda:[SimpleNamespace(intent=SimpleNamespace(symbol='ORDR'))])
    delivery.synchronize(s.manager,broker,s.clock.now)
    generation=feed._generation
    assert set(feed._desired)==set(s.manager.base_symbols(s.clock.now))|{'ORDR'}
    delivery.synchronize(s.manager,broker,s.clock.now)
    assert feed._generation==generation
    s.clock.now=NOW+timedelta(days=1);s.caps['T050']='3000'
    await collect(s)
    feed._acked=True
    delivery.synchronize(s.manager,broker,s.clock.now)
    assert {'T049','T050','ORDR'}<=set(feed._desired)
    assert feed._generation>generation and not feed._acked
    assert not delivery.entry_allowed('T050',s.clock.now)


async def test_official_catalog_route_reuses_token_and_reads_only():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'result':[stock('AAPL')]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=TossMarketClient(client=http);client._token='fixture';client._token_deadline=float('inf')
        async def immediate():pass
        client.limiter.wait=immediate
        assert (await client.stocks_all('NASDAQ'))[0]['symbol']=='AAPL'
        with pytest.raises(ValueError):await client.stocks_all('US_ETC')
    assert len(calls)==1 and calls[0].method=='GET' and calls[0].url.path=='/api/v1/stocks/all'
    assert dict(calls[0].url.params)=={'market':'NASDAQ','status':'ACTIVE','commonShare':'true'}
    assert calls[0].headers['authorization']=='Bearer fixture'


@pytest.mark.parametrize('at',[datetime(2026,11,21,12,tzinfo=timezone.utc),NOW.replace(hour=22)])
async def test_no_new_selection_on_weekend_or_after_close(setup,at):
    s=setup;s.clock.now=at
    assert not await s.provider.advance(s.read)
    assert not s.calls


async def test_early_close_and_us_trading_date(setup):
    s=setup;s.clock.now=datetime(2026,11,27,12,tzinfo=timezone.utc)
    result=await collect(s)
    assert result['snapshot']['valid_until']=='2026-11-27T18:00:00+00:00'
    s.clock.now=datetime(2026,11,28,1,tzinfo=timezone.utc)
    assert s.manager.base_symbols(s.clock.now)==[]
    assert not await s.provider.advance(s.read)
    assert len(s.calls)==5


def test_universe_cache_preserves_historical_membership_expiry_and_caller_isolation(tmp_path):
    universe=UniverseStore(tmp_path/'cache.json',max_age_days=1)
    universe.refresh(['A'],source='fixture',observed_at=NOW)
    universe.members(NOW).clear()
    assert universe.contains('A',NOW)
    universe.refresh(['B'],source='fixture',observed_at=NOW+timedelta(hours=1))
    assert universe.contains('B',NOW+timedelta(hours=2)) and universe.contains('A',NOW)
    assert universe.members(NOW+timedelta(days=2))=={}


async def test_runtime_starts_empty_us_catalog_without_requesting_sp500(setup,monkeypatch):
    from dataclasses import replace
    from sessionquant.market.source import MarketBatch
    from sessionquant.session_runtime import run_session
    s=setup;calls=[]
    async def no_sp500(*args,**kwargs):
        pytest.fail('US profile requested S&P 500')
    monkeypatch.setattr(UniverseStore,'refresh_public',no_sp500)
    class Source:
        def __init__(self,**kwargs):self.watchlist=kwargs['watchlist']
        async def poll(self,symbols,**kwargs):
            calls.append(symbols)
            return MarketBatch()
        async def aclose(self):pass
    monkeypatch.setattr('sessionquant.session_runtime.TossSessionSource',Source)
    config=replace(s.config,news_enabled=False,actual_account_monitor_enabled=False)
    assert await run_session(config,no_dashboard=True,max_cycles=1,desktop_notifications=False)==0
    assert calls==[[]] and not s.repo.all('orders')
    assert s.repo.load_state()['broker']['cash']=='100'


async def test_managed_collector_uses_existing_client_and_one_catalog_request_per_cycle(setup):
    from sessionquant.market.source import TossSessionSource
    s=setup
    class Client:
        async def stocks_all(self,market):return await s.read('stocks_all',market)
        async def sectors(self):return await s.read('sectors',None)
        async def sector_stocks(self,sector):return await s.read('sector_stocks',sector)
        async def prices(self,symbols):return []
        async def rankings(self,**kwargs):return {'rankings':[],'rankedAt':None}
        async def stocks(self,symbols):return []
    source=TossSessionSource(Client(),watchlist=s.manager,clock=lambda:s.clock.now,
                             include_benchmarks=False,current_session_only=True)
    for index in range(3):
        await source.poll([])
        assert len(s.calls)==index+1
    assert s.calls==[('stocks_all',market) for market in MARKETS]
    assert s.provider.us_catalog.ready(s.clock.now)


@pytest.mark.parametrize('fault',['malformed','rate_limit'])
async def test_catalog_failure_reports_and_recovers_without_publishing_partial_candidates(setup,fault):
    from sessionquant.market.source import TossSessionSource
    s=setup;attempts=[]
    class Client:
        async def stocks_all(self,market):
            attempts.append(market)
            if len(attempts)==1:
                if fault=='malformed':return [{'symbol':'INVALID'}]
                response=httpx.Response(429,headers={'Retry-After':'60'},request=httpx.Request('GET','https://fixture.invalid/catalog'))
                response.raise_for_status()
            return await s.read('stocks_all',market)
        async def sectors(self):return await s.read('sectors',None)
        async def sector_stocks(self,sector):return await s.read('sector_stocks',sector)
        async def prices(self,symbols):return []
        async def rankings(self,**kwargs):return {'rankings':[],'rankedAt':None}
        async def stocks(self,symbols):return []
    source=TossSessionSource(Client(),watchlist=s.manager,clock=lambda:s.clock.now,
                             include_benchmarks=False,current_session_only=True)
    batch=await source.poll([])
    assert batch.errors and all({'source','symbol','error_type','message'}<=row.keys() for row in batch.errors)
    assert not s.manager.base_symbols(s.clock.now) and not s.universe.snapshots
    assert not s.provider.us_catalog.state['pages']
    if fault=='rate_limit':
        assert 'STOCK_ALL' in s.manager.state['reference_backoff_groups']
        await source.poll([])
        assert attempts==['NYSE']
        s.clock.now+=timedelta(seconds=61)
    for _ in MARKETS:await source.poll([])
    assert attempts==['NYSE',*MARKETS] and s.provider.us_catalog.ready(s.clock.now)
    assert not s.repo.all('orders')
