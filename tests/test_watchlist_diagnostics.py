from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from sessionquant.market.history_commands import collection_selection
from sessionquant.watchlist.capitalization import WatchlistSelector
from sessionquant.watchlist.diagnostics import diagnose
from sessionquant.watchlist.toss_capitalization import STATE
from watchlist_fixtures import context,NOW
from test_toss_capitalization import collect


async def partial(tmp_path):
    repo,universe,config,provider,manager=context(tmp_path,size=50)
    _,batch,_=await collect(repo,universe)
    for page in batch['pages'].values():
        page['stocks']=[row for row in page['stocks'] if row['symbol']!='T499']
    snapshot=WatchlistSelector(50).select(batch,universe,NOW)
    manager.store.add(snapshot)
    state=repo.get_control(STATE)
    state['pages']=batch['pages']
    repo.set_control(STATE,state)
    return repo,manager


async def test_partial_can_only_prepare_bars_explicitly_without_certifying_rank(tmp_path):
    repo,manager=await partial(tmp_path)
    try:
        before=deepcopy(manager.store.at(NOW))
        with pytest.raises(ValueError):collection_selection(manager,NOW)
        symbols,selection=collection_selection(manager,NOW,allow_partial=True)
        assert len(symbols)==50 and symbols[0]=='T498' and 'T499' not in symbols
        assert selection['status']=='PARTIAL' and selection['collection_only'] and not selection['ranking_verified']
        assert selection['missing_market_cap_symbols']==['T499']
        assert manager.light_symbols(NOW)==manager.focus(NOW)==[]
        assert manager.entry_blockers(symbols[0],NOW)==['watchlist_partial']
        assert manager.store.at(NOW)==before and repo.all('orders')==[]
        with pytest.raises(ValueError):collection_selection(manager,NOW+timedelta(days=1),allow_partial=True)
    finally:repo.close()


async def test_partial_conflicts_cannot_use_missing_only_option(tmp_path):
    repo,manager=await partial(tmp_path)
    try:
        from sessionquant.watchlist.capitalization import HISTORY
        rows=repo.get_control(HISTORY)
        rows[-1]['issues'].append({'ticker':'T498','code':'sector_cap_conflict'})
        repo.set_control(HISTORY,rows)
        with pytest.raises(ValueError):collection_selection(manager,NOW,allow_partial=True)
    finally:repo.close()


async def test_diagnostic_separates_sector_omission_from_valid_stock_lookup(tmp_path):
    repo,manager=await partial(tmp_path)
    class Client:
        calls=[]
        async def stocks(self,symbols):
            self.calls.append(symbols)
            return [{'symbol':'T499','status':'ACTIVE','currency':'USD','market':'NASDAQ','sharesOutstanding':'123',
                     'accountSeq':'must-not-appear','access_token':'must-not-appear'}]
    try:
        offline=await diagnose(repo,manager,now=NOW)
        assert offline['items'][0]['stock_lookup']=='NOT_REQUESTED'
        result=await diagnose(repo,manager,client=Client(),now=NOW)
        assert result['items'][0]['sector_status']=='ABSENT_FROM_SECTOR_PAGES'
        assert result['items'][0]['stock_lookup']=='FOUND' and Client.calls==[['T499']]
        assert 'must-not-appear' not in str(result)
        assert not result['ranking_modified'] and manager.store.status(NOW)['status']=='PARTIAL'
    finally:repo.close()


async def test_lookup_error_is_sanitized_and_not_a_delisting_claim(tmp_path):
    repo,manager=await partial(tmp_path)
    class Client:
        async def stocks(self,symbols):
            httpx.Response(404,request=httpx.Request('GET','https://example.invalid?token=SECRET')).raise_for_status()
    try:
        result=await diagnose(repo,manager,client=Client(),now=NOW)
        row=result['items'][0]
        assert row['stock_lookup']=='ERROR' and row['http_status']==404
        assert 'stock' not in row and 'SECRET' not in str(result)
    finally:repo.close()
