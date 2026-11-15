"""Resumable pre-session backfill for the selected watchlist, with a bounded API budget."""
import os
from datetime import datetime, timezone

import httpx

from ..collectors.toss_market import TossMarketClient
from ..models import utc
from ..watchlist.budget import ReadCallBudget, retry_budget, provider_retry_until
from .history import HistoryStore, history_plan, VIEW

PROGRESS = 'watchlist_history_backfill_v1'


async def backfill(repo, symbols, client, *, now=None, max_requests=5000, refresh=False, progress_fn=None,
                   selection_context=None):
    now = utc(now or datetime.now(timezone.utc))
    if not symbols or len(symbols) > 100 or len(set(symbols)) != len(symbols):
        raise ValueError('검증된 감시 종목 1~100개가 필요합니다.')
    if type(max_requests) is not int or not 1 <= max_requests <= 20000:
        raise ValueError('최대 요청 수는 1~20000입니다.')
    prior = repo.get_control(PROGRESS,{}) or {}
    if prior.get('retry_after') and now < utc(prior['retry_after']):
        raise ValueError('공급자 재시도 대기 중입니다: '+prior['retry_after'])
    store = HistoryStore(repo)
    plan = history_plan(now,include_current=True)
    budget = ReadCallBudget(max_requests)
    token = retry_budget.set(budget)
    report = {'started_at':now.isoformat(),'symbols':symbols,'required_full_sessions':20,
              'session_dates':[r[0] for r in plan], 'complete_symbol_days':0, 'skipped_symbol_days':0,
              'required_symbol_days':len(plan)*len(symbols), 'read_requests':0,
              'status':'RUNNING','error':None,'retry_after':None,
              'selection':selection_context or {'status':'UNSPECIFIED','ranking_verified':False}}
    try:
        for index,symbol in enumerate(symbols,1):
            for day,opened,closed in plan:
                row,bars = store.get(symbol,day)
                if row and row['quality']['complete'] and not refresh:
                    report['complete_symbol_days'] += 1
                    report['skipped_symbol_days'] += 1
                    continue
                saved = (row or {}).get('progress',{})
                resumable = not refresh and saved.get('status') == 'PAGING' and saved.get('cursor')
                cursor = utc(saved['cursor']) if resumable else closed
                pages = saved.get('pages',0) if resumable else 0
                # Each request and its next cursor are committed together. Ctrl+C is resumable.
                for _ in range(10):
                    report.update(current_symbol=symbol,current_session=day)
                    budget.take()
                    fetched, following = await client.candles(symbol,count=200,before=cursor)
                    if any(bar.timestamp > cursor for bar in fetched):
                        raise ValueError('과거 분봉 페이지가 요청 상한을 넘었습니다.')
                    pages += 1
                    reached = bool(fetched and min(b.timestamp for b in fetched) <= opened)
                    stalled = bool(following and utc(following) >= cursor)
                    selected = [b for b in fetched if opened < b.timestamp <= closed]
                    status = ('CURSOR_STALLED' if stalled else 'SESSION_START_REACHED' if reached
                              else 'HISTORY_ENDED' if not fetched or not following else 'PAGING')
                    row = store.save(symbol,day,selected,progress={'cursor':following,'pages':pages,'status':status})
                    if row['quality']['complete']:
                        report['complete_symbol_days'] += 1
                        break
                    if status != 'PAGING':
                        break
                    cursor = utc(following)
                report['read_requests'] = budget.used
                repo.set_control(PROGRESS,report)
                if progress_fn:
                    q = row['quality']
                    progress_fn(f"[{index}/{len(symbols)}] {symbol} {day}: {q['received_minutes']}/{q['expected_minutes']}분, "
                                f"누락 {q['missing_minutes']}, 충돌 {q['conflicting_minutes']} · 요청 {budget.used}")
            # Keep memory bounded while loading 100 independent symbol histories.
            store.cache.clear()
        report['status'] = 'COMPLETE' if report['complete_symbol_days'] == report['required_symbol_days'] else 'INCOMPLETE'
    except Exception as exc:
        report['status'] = 'INTERRUPTED'
        report['error'] = {'type':type(exc).__name__}
        if isinstance(exc,httpx.HTTPStatusError):
            report['error']['http_status'] = exc.response.status_code
            if exc.response.status_code == 429:
                report['retry_after'] = provider_retry_until(exc.response.headers.get('Retry-After'),datetime.now(timezone.utc)).isoformat()
    finally:
        retry_budget.reset(token)
        report['read_requests'] = budget.used
        if report['status'] == 'RUNNING':
            report['status'] = 'INTERRUPTED'
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        repo.set_control(PROGRESS,report)
    return report


def collection_selection(manager, now, *, allow_partial=False):
    """Incomplete ranking may prepare bars explicitly; it never creates a trading watchlist."""
    from ..market_reference.contracts import REFERENCE_SYMBOLS
    from ..models import symbol_valid
    from ..watchlist.toss_capitalization import CONTRACT
    from ..watchlist.capitalization import EXCLUDABLE_CAP_ISSUES
    status = manager.store.status(now)
    snapshot = status.get('snapshot') or {}
    issues = snapshot.get('issues',[])
    partial = (allow_partial and status['status']=='PARTIAL' and snapshot.get('contract')==CONTRACT
               and snapshot.get('synthetic') is False and issues
               and all(row.get('code')=='market_cap_missing' for row in issues))
    if status['status']=='VALID':
        symbols=manager.base_symbols(now)
    elif partial:
        members=manager.universe.members(now)
        symbols=[row['ticker'] for row in status['items'][:manager.config.watchlist_size]
                 if row['ticker'] in members and row['ticker'] not in REFERENCE_SYMBOLS]
        missing={row.get('ticker') for row in issues}
        if set(symbols)&missing:
            raise ValueError('시총 미확인 종목이 사전 수집 후보에 포함되어 있습니다.')
    else:
        raise ValueError('유효한 감시목록이 없습니다. watchlist-diagnose로 확인하세요. 시총 누락만 있는 PARTIAL은 --allow-partial-watchlist로 분봉 사전 수집만 허용할 수 있습니다.')
    if not 1<=len(symbols)<=manager.config.watchlist_size or len(set(symbols))!=len(symbols) or (partial and len(symbols)!=manager.config.watchlist_size):
        raise ValueError('설정한 상한 이내의 서로 다른 수집 후보가 필요합니다. PARTIAL 사전 수집은 설정한 수만큼 필요합니다.')
    for symbol in symbols:symbol_valid(symbol)
    return symbols,{'status':status['status'],'ranking_verified':status['status']=='VALID' and snapshot.get('ranking_verified',True),
        'snapshot_id':snapshot.get('id'),'collection_only':bool(partial),
        'ranking_scope':snapshot.get('ranking_scope','full_universe'),
        'excluded_market_cap_symbols':snapshot.get('excluded_market_cap_symbols',[]),
        'missing_market_cap_symbols':sorted({row['ticker'] for row in issues if row.get('code') in EXCLUDABLE_CAP_ISSUES})}


async def command(config, action, *, max_requests=5000, refresh=False, allow_partial=False, details=False, output=None):
    from ..session_runtime import _open, bind_runtime
    from ..database.repository import Repository
    from ..watchlist.selection import WatchlistManager
    from .universe import UniverseStore
    os.environ['SQ_ENVIRONMENT'] = 'PAPER_LOCAL'
    os.environ['SQ_DB_BACKEND'] = config.database_backend
    if action == 'history-status':
        repo = Repository(config.db_path,backend=config.database_backend)
        try:
            from .history import current_view
            from ..watchlist.history_selection import CONTROL, inspect_history
            report = {'backfill':repo.get_control(PROGRESS,{}),'rvol':current_view(repo.get_control(VIEW,{})),
                    'daily_watchlist':repo.get_control(CONTROL,{}),
                    'order_writes':0,'llm_generation_calls':0}
            if details or output is not None:
                from .history_diagnostics import diagnose_history
                report['history_quality']=diagnose_history(repo,report['backfill'])
                manager = WatchlistManager(repo,UniverseStore(config.universe_path),config)
                now = datetime.now(timezone.utc)
                report['history_readiness_preview'] = inspect_history(HistoryStore(repo),manager.base_symbols(now),now)
                report['history_readiness_preview']['watchlist_status'] = manager.store.status(now)['status']
            if output is not None:
                import json
                from pathlib import Path
                target=Path(output)
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('x',encoding='utf-8') as handle:
                    json.dump(report,handle,ensure_ascii=False,indent=2)
                preview = report['history_readiness_preview']
                return {'output':str(target),'history_quality':report['history_quality']['summary'],
                        'history_ready_symbols':preview['included_symbols'],
                        'history_excluded_count':preview['excluded_count'],
                        'provider_calls':0,'order_writes':0,'llm_generation_calls':0}
            return report
        finally:
            repo.close()
    if config.source != 'toss' or not config.watchlist_enabled:
        raise ValueError('토스 감시목록을 사용하는 설정이 필요합니다.')
    if not all(os.getenv(key) for key in ('TOSS_CLIENT_ID','TOSS_CLIENT_SECRET')):
        raise ValueError('TOSS_CLIENT_ID와 TOSS_CLIENT_SECRET 환경변수가 필요합니다.')
    stack,repo = _open(config.db_path,config.database_backend)
    with stack:
        from ..connection_checks import active_collector
        if active_collector(repo):
            raise ValueError('토스 수집기·계좌 조회기를 종료한 뒤 실행하세요.')
        manager = WatchlistManager(repo,UniverseStore(config.universe_path),config)
        symbols,selection = collection_selection(manager,datetime.now(timezone.utc),allow_partial=allow_partial)
        bind_runtime(repo,source='toss',identity='toss-readonly')
        client = TossMarketClient(account_seq=0)
        print('같은 클라이언트의 토스 수집기를 종료한 상태에서 실행하세요. 인증 외에는 분봉 GET만 사용합니다.',flush=True)
        if selection['collection_only']:
            print('PARTIAL 후보의 분봉 사전 수집입니다. 상위 순위 확정·감시목록 활성화·매수 허용은 하지 않습니다.',flush=True)
        try:
            result = await backfill(repo,symbols,client,max_requests=max_requests,refresh=refresh,
                                    selection_context=selection,
                                    progress_fn=lambda text:print(text,flush=True))
        finally:
            await client.aclose()
        return result
