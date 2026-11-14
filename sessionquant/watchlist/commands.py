"""감시목록 읽기·검증·내보내기 명령. 증권사·LLM·주문 API는 호출하지 않는다."""
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
            result = await manager.refresh(now, force=True)
            manager.publish(now)
            return {'status':result['status'], 'watchlist_size':len(result['items']), 'refresh':result['refresh'],
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
