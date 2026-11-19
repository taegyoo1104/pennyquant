"""감시목록 조회·검증·내보내기. 갱신만 토스 읽기 API를 사용할 수 있다."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..market.universe import UniverseStore
from .capitalization import HISTORY
from .selection import VIEW, WatchlistManager


async def command(config, action, *, output=None):
    from ..session_runtime import _open
    from ..database.repository import Repository
    os.environ['SQ_ENVIRONMENT'] = 'PAPER_LOCAL'
    os.environ['SQ_DB_BACKEND'] = config.database_backend
    universe = UniverseStore(config.universe_path)
    now = datetime.now(timezone.utc)
    if action == 'watchlist-refresh':
        # 기존 엔진의 쓰기 잠금을 공유한다. 동작 중인 작성자와 경쟁하지 않는다.
        stack, repo = _open(config.db_path, config.database_backend)
        with stack:
            manager = WatchlistManager(repo, universe, config)
            from ..watchlist.toss_capitalization import bind_provider
            from ..collectors.toss_market import TossMarketClient
            from ..connection_checks import active_collector
            client=TossMarketClient()
            try:
                if bind_provider(manager,client):
                    if active_collector(repo):
                        raise ValueError('토스 엔진·계좌 조회기를 종료한 뒤 갱신하세요. 인증 토큰 충돌을 방지합니다.')
                    from .budget import ReadCallBudget,retry_budget
                    budget=ReadCallBudget(64)
                    token=retry_budget.set(budget)
                    try:
                        async def read(stream,symbol):
                            budget.take()
                            if stream=='stocks_all':return await client.stocks_all(symbol)
                            return await client.sectors() if stream=='sectors' else await client.sector_stocks(symbol)
                        # 계층 1회 + 대분류별 1회. 호출 제한·실패 시 재전송하지 않고 종료한다.
                        import asyncio
                        async def collect():
                            for _ in range(32):
                                if await manager.provider.advance(read,limit=1):break
                        await asyncio.wait_for(collect(),timeout=90)
                    finally:retry_budget.reset(token)
                result = await manager.refresh(datetime.now(timezone.utc), force=True)
            finally:await client.aclose()
            now=datetime.now(timezone.utc)
            manager.publish(now)
            return {'status':result['status'], 'watchlist_size':len(result['items']), 'refresh':result['refresh'],
                    'ranking_verified':result.get('ranking_verified',False),
                    'ranking_scope':result.get('ranking_scope'),
                    'market_cap_coverage':result.get('market_cap_coverage'),
                    'excluded_market_cap_symbols':result.get('excluded_market_cap_symbols',[]),
                    'live_orders_enabled':False}
    repo = Repository(config.db_path, backend=config.database_backend)
    try:
        if action == 'watchlist-export':
            target = Path(output)
            if target.exists():
                raise ValueError('내보내기 파일이 존재합니다. 새 경로를 지정하세요.')
            target.parent.mkdir(parents=True, exist_ok=True)
            history = {'schema_version':1, 'snapshots':repo.get_control(HISTORY, []),
                'focus_history':[r for r in repo.all('pipeline_status') if r.get('kind') == 'watchlist_focus']}
            # 새 파일만 작성하여 기존 내보내기를 덮어쓰지 않는다.
            with target.open('x', encoding='utf-8') as file:
                json.dump(history, file, ensure_ascii=False, indent=2)
            return {'output':str(target), 'snapshots':len(history['snapshots']), 'focus_frames':len(history['focus_history']),
                    'live_orders_enabled':False}
        view = repo.get_control(VIEW)
        manager = WatchlistManager(repo, universe, config)
        current = manager.store.status(now)
        if view:
            from .capitalization import utc
            active = bool(view.get('updated_at') and 0 <= (now-utc(view['updated_at'])).total_seconds() <= config.watchlist_data_max_age_seconds)
            return {**view, **current, 'focus_count':len(manager.focus(now)) if active else 0,
                    'observation_status':'CURRENT' if active else 'STALE_OR_STOPPED'}
        return current
    finally:
        repo.close()
