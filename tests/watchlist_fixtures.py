"""실제 기업·시총·계좌를 나타내지 않는 감시목록 전용 합성 입력."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sessionquant.database.repository import Repository
from sessionquant.market.universe import UniverseStore
from sessionquant.session_config import SessionConfig
from sessionquant.watchlist.selection import WatchlistManager

NOW = datetime(2026,11,13,16,tzinfo=timezone.utc)


def cap_batch(count=500):
    stamp=(NOW-timedelta(hours=1)).isoformat()
    return {'schema_version':1,'synthetic':True,'source':'SYNTHETIC_FIXTURE',
        'source_verified':True,'source_documentation':'합성 검증용 정의·입력. 실제 데이터가 아님.',
        'currency':'USD','market_cap_basis':'issuer_total','as_of_at':stamp,'received_at':stamp,'known_at':stamp,
        'observations':[{'ticker':f'T{i:03}', 'issuer_id':f'FIXTURE_ISSUER_{i:03}',
            'issuer_verified':True,'market_cap_usd':str((i+1)*1_000_000),'data_status':'VERIFIED'} for i in range(count)]}


class Provider:
    def __init__(self,batch):self.batch,self.calls=batch,0
    async def fetch(self):
        self.calls+=1
        return deepcopy(self.batch)


def context(tmp_path,count=500,size=100):
    repo=Repository(tmp_path/'watch.db',backend='sqlite')
    universe=UniverseStore(tmp_path/'members.json')
    universe.refresh([f'T{i:03}' for i in range(count)],source='SYNTHETIC_FIXTURE',observed_at=NOW-timedelta(days=1))
    config=SessionConfig(database_backend='sqlite',db_path=repo.path,universe_path=tmp_path/'members.json',
        company_rss_catalog=False,market_cap_input=tmp_path/'caps.json',watchlist_size=size,focus_size=min(size,20))
    provider=Provider(cap_batch(count))
    manager=WatchlistManager(repo,universe,config,provider=provider,clock=lambda:NOW,allow_synthetic=True)
    return repo,universe,config,provider,manager


def observe(manager):
    symbols=manager.light_symbols(NOW)
    first=[{'symbol':s,'lastPrice':'100','timestamp':NOW.isoformat(),'currency':'USD'} for s in [*symbols,'SPY','QQQ']]
    after=NOW+timedelta(seconds=30)
    second=[{'symbol':s,'lastPrice':str(101 if s.startswith('T') else 100),
             'timestamp':after.isoformat(),'currency':'USD'} for s in [*symbols,'SPY','QQQ']]
    manager.observe_prices(first,NOW)
    manager.observe_prices(second,after)
    manager.observe_rankings({'rankedAt':after.isoformat(),'rankings':[{'symbol':s,'currency':'USD',
        'tradingAmount':str((1000-int(s[1:]))*1_000_000),'tradingVolume':'100000'} for s in symbols]},after)
    return after
