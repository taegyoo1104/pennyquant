"""Explain stored market-cap gaps; optional exact-symbol StockInfo GETs do not repair ranks."""
import os
from datetime import datetime, timezone

import httpx

from .toss_capitalization import STATE
from .capitalization import EXCLUDABLE_CAP_ISSUES


async def diagnose(repo,manager,*,client=None,now=None):
    now=now or datetime.now(timezone.utc)
    status=manager.store.status(now)
    snapshot=status.get('snapshot') or {}
    collection=repo.get_control(STATE,{}) or {}
    pages=collection.get('pages',{})
    missing=sorted({r['ticker'] for r in snapshot.get('issues',[]) if r.get('code') in EXCLUDABLE_CAP_ISSUES})
    result={'status':status['status'],'candidate_count':len(status['items']),
        'universe_count':status['universe_count'],'missing_market_cap_symbols':missing,
        'ranking_verified':status.get('ranking_verified',False),
        'ranking_scope':status.get('ranking_scope'),
        'excluded_market_cap_symbols':snapshot.get('excluded_market_cap_symbols',[]),
        'collection_complete':collection.get('complete',False),
        'sector_pages_received':len(pages),'sector_pages_required':len(collection.get('roots') or []),
        'collection_started_at':collection.get('started_at'),'collection_completed_at':collection.get('completed_at'),
        'issues':snapshot.get('issues',[]),'items':[],'connected':client is not None,
        'order_writes':0,'llm_generation_calls':0,'ranking_modified':False,
        'lookup_limit':20,'lookup_truncated':len(missing)>20}
    for symbol in missing[:20]:
        matches=[{'sector_id':sector,'currency':row.get('currency'),'market_cap':row.get('marketCapitalization')}
                 for sector,page in pages.items() for row in page.get('stocks',[]) if row.get('symbol')==symbol]
        row={'symbol':symbol,'sector_matches':matches,
             'sector_status':'PRESENT_WITHOUT_USABLE_CAP' if matches else 'ABSENT_FROM_SECTOR_PAGES',
             'stock_lookup':'NOT_REQUESTED'}
        if client is not None:
            try:
                response=await client.stocks([symbol])
                exact=[item for item in response if isinstance(item,dict) and item.get('symbol')==symbol] if isinstance(response,list) else []
                if len(exact)==1:
                    row['stock_lookup']='FOUND'
                    row['stock']={key:exact[0].get(key) for key in ('symbol','name','englishName','isinCode',
                        'market','currency','status','securityType','isCommonShare','sharesOutstanding','listDate','delistDate')}
                else:row['stock_lookup']='EXACT_SYMBOL_NOT_CONFIRMED'
            except Exception as exc:
                row['stock_lookup']='ERROR'
                row['error_type']=type(exc).__name__
                if isinstance(exc,httpx.HTTPStatusError):
                    row['http_status']=exc.response.status_code
                    if exc.response.status_code in {401,403,429}:
                        result['items'].append(row)
                        result['lookup_stopped']=True
                        break
        result['items'].append(row)
    return result


async def command(config,*,connect=False):
    from ..session_runtime import _open
    from ..database.repository import Repository
    from ..market.universe import UniverseStore
    from .selection import WatchlistManager
    from ..collectors.toss_market import TossMarketClient
    from ..connection_checks import active_collector
    os.environ['SQ_ENVIRONMENT']='PAPER_LOCAL'
    os.environ['SQ_DB_BACKEND']=config.database_backend
    if not connect:
        repo=Repository(config.db_path,backend=config.database_backend)
        try:
            return await diagnose(repo,WatchlistManager(repo,UniverseStore(config.universe_path),config))
        finally:repo.close()
    if not all(os.getenv(key) for key in ('TOSS_CLIENT_ID','TOSS_CLIENT_SECRET')):
        raise ValueError('토스 인증 환경변수가 필요합니다.')
    stack,repo=_open(config.db_path,config.database_backend)
    with stack:
        if active_collector(repo):raise ValueError('토스 수집기·계좌 조회기를 종료한 뒤 진단하세요.')
        client=TossMarketClient(account_seq=0)
        try:
            return await diagnose(repo,WatchlistManager(repo,UniverseStore(config.universe_path),config),client=client)
        finally:await client.aclose()
