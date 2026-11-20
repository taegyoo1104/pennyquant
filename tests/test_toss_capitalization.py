"""공식 섹터 시총 계약·증분 조회·누락·원화·과거 시점 호환을 검증한다."""
from copy import deepcopy
from datetime import timedelta
from dataclasses import replace

import httpx
import pytest

from sessionquant.collectors.toss_market import TossMarketClient
from sessionquant.watchlist.capitalization import WatchlistSelector
from sessionquant.watchlist.toss_capitalization import TossMarketCapProvider,CollectionPending,bind_provider
from sessionquant.watchlist.selection import WatchlistManager
from sessionquant.dashboard.watchlist_panel import current_status
from watchlist_fixtures import NOW,context


def sector_rows(count=500):
    return [{'symbol':f'T{i:03}','name':f'한글 종목 {i}','marketCapitalization':str((i+1)*1000000),'currency':'KRW'} for i in range(count)]


async def collect(repo,universe):
    provider=TossMarketCapProvider(repo,universe,clock=lambda:NOW)
    calls=[]
    async def read(stream,symbol):
        calls.append((stream,symbol))
        if stream=='sectors':return {'sectors':[{'sectorId':'b','depth':0},{'sectorId':'a','depth':0},
            {'sectorId':'aa','depth':1,'parentSectorId':'a'}]}
        return {'stocks':sector_rows() if symbol=='a' else sector_rows()[-20:]}
    await provider.advance(read)
    with pytest.raises(CollectionPending):await provider.fetch()
    # 재시작 뒤에도 이미 읽은 계층을 다시 요청하지 않는다.
    provider=TossMarketCapProvider(repo,universe,clock=lambda:NOW)
    assert not await provider.advance(read)
    assert await provider.advance(read)
    return provider,await provider.fetch(),calls


async def test_rank_100_krw_dedup_restart_and_no_future_history(tmp_path):
    repo,universe,config,_,manager=context(tmp_path)
    try:
        provider,batch,calls=await collect(repo,universe)
        assert calls==[('sectors',None),('sector_stocks','a'),('sector_stocks','b')]
        manager.provider=provider
        result=await manager.refresh(NOW,force=True)
        assert result['status']=='VALID'
        assert [r['ticker'] for r in result['items']]==[f'T{i:03}' for i in range(499,399,-1)]
        assert all(r['market_cap_usd'] is None and not r['issuer_verified'] for r in result['items'])
        assert result['snapshot']['currency']=='KRW' and result['snapshot']['provider_as_of_at'] is None
        assert result['items'][0]['sector_ids']==['a','b']
        assert result['items'][0]['name']=='한글 종목 499'
        assert manager.store.at(NOW-timedelta(seconds=1)) is None
        assert manager.store.status(NOW+timedelta(days=1))['status']=='EXPIRED'
        manager.publish(NOW)
        state=repo.get_control('watchlist_dashboard_v1')
        assert state['items'][0]['name']=='한글 종목 499'
        assert current_status(state,NOW+timedelta(days=1))[0]=='EXPIRED'
        assert repo.all('orders')==[]
    finally:repo.close()


@pytest.mark.parametrize('bad',[None,'NaN','0','-1',True])
async def test_missing_bad_cap_never_certifies_top100(tmp_path,bad):
    repo,universe,_,_,_=context(tmp_path)
    try:
        _,batch,_=await collect(repo,universe)
        for page in batch['pages'].values():
            for r in page['stocks']:
                if r['symbol']=='T499':r['marketCapitalization']=bad
        snapshot=WatchlistSelector().select(batch,universe,NOW)
        assert snapshot.payload['data_status']=='PARTIAL'
        assert 'T499' not in snapshot.symbols
    finally:repo.close()


async def test_conflict_omission_currency_and_unknown_endpoint_contract(tmp_path):
    repo,universe,_,_,_=context(tmp_path)
    try:
        _,batch,_=await collect(repo,universe)
        for change in ('conflict','omission','currency'):
            b=deepcopy(batch)
            if change=='conflict':b['pages']['b']['stocks'][-1]['marketCapitalization']='9'
            if change=='omission':b['pages']['a']['stocks'].pop(0)
            if change=='currency':b['pages']['a']['stocks'][0]['currency']='USD'
            assert WatchlistSelector().select(b,universe,NOW).payload['data_status']=='PARTIAL'
        for key,value in [('contract','unknown'),('provider_as_of_at',NOW.isoformat()),('root_sectors',['a'])]:
            b=deepcopy(batch);b[key]=value
            with pytest.raises(ValueError):WatchlistSelector().select(b,universe,NOW)
    finally:repo.close()


async def test_root_graph_must_have_correct_depth_and_no_cycle(tmp_path):
    repo,universe,_,_,_=context(tmp_path)
    try:
        provider=TossMarketCapProvider(repo,universe,clock=lambda:NOW)
        async def read(*_):return {'sectors':[{'sectorId':'a','depth':0},{'sectorId':'b','depth':2,'parentSectorId':'a'}]}
        with pytest.raises(ValueError):await provider.advance(read)
    finally:repo.close()


def test_auto_preserves_injected_and_existing_file_providers(tmp_path):
    repo,universe,config,_,manager=context(tmp_path)
    try:
        client=type('Client',(),{'sectors':None,'sector_stocks':None})()
        assert not bind_provider(manager,client)
        manager=WatchlistManager(repo,universe,config,clock=lambda:NOW)
        assert bind_provider(manager,client)
        config.market_cap_input.write_text('{}')
        manager=WatchlistManager(repo,universe,config)
        assert not bind_provider(manager,client)
        manager=WatchlistManager(repo,universe,replace(config,market_cap_provider='toss'))
        assert bind_provider(manager,client)
    finally:repo.close()


async def test_official_sector_routes_use_same_token_and_get_only():
    calls=[]
    async def handler(request):
        calls.append(request)
        result={'sectors':[]} if request.url.path=='/api/v1/sectors' else {'stocks':[]}
        return httpx.Response(200,json={'result':result})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=TossMarketClient(client=http)
        client._token='fixture';client._token_deadline=float('inf')
        async def immediate():pass
        client.limiter.wait=immediate
        await client.sectors();await client.sector_stocks('A1')
        with pytest.raises(ValueError):await client.sector_stocks('../accounts')
    assert [r.method for r in calls]==['GET','GET']
    assert calls[-1].url.params['marketCountry']=='US'
    assert all(r.headers['authorization']=='Bearer fixture' for r in calls)
