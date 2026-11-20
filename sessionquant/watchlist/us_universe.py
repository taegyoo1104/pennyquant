"""Daily, complete-exchange universe from Toss's documented read-only catalog."""
from datetime import datetime, timedelta, timezone

from ..market.bars import session_bounds
from ..models import symbol_valid, utc
from .capitalization import symbols_digest

MARKETS = ('NYSE', 'NASDAQ', 'AMEX')
STOCK_TYPES = {'STOCK', 'FOREIGN_STOCK', 'DEPOSITARY_RECEIPT', 'REIT'}
SOURCE = 'toss:/api/v1/stocks/all:NYSE,NASDAQ,AMEX:active-common:v1.2.28'
POLICY = 'toss_us_daily_fixed_v1'
STATE = 'toss_us_universe_collection_v1'


def selection_session(now):
    """Use the exchange's trading date, including DST/early closes; no holiday selection."""
    bounds = session_bounds(utc(now))
    return bounds if bounds and utc(now) < bounds[1] else None


def catalog_members(pages):
    if set(pages) != set(MARKETS):
        raise ValueError('미국 거래소 목록을 모두 확인하지 못했습니다.')
    members, identities = {}, {}
    for market in MARKETS:
        for row in catalog_page(pages[market]['stocks']):
            symbol = row['symbol']
            identity = (row['isinCode'], row['securityType'])
            if symbol in identities and identities[symbol] != identity:
                raise ValueError('거래소 간 종목 식별 정보가 충돌합니다.')
            identities[symbol] = identity
            members[symbol] = {'symbol': symbol, 'name': row['name'], 'sector': ''}
    if not members:
        raise ValueError('확인된 미국 상장 보통주가 없습니다.')
    return list(members.values())


def catalog_page(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20000:
        raise ValueError('미국 종목 목록 응답을 확인하세요.')
    seen, accepted = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('미국 종목 목록 형식을 확인하세요.')
        symbol = row.get('symbol')
        symbol_valid(symbol)
        if symbol in seen:
            raise ValueError('같은 거래소에 중복 종목이 있습니다.')
        seen.add(symbol)
        if type(row.get('isCommonShare')) is not bool or not isinstance(row.get('securityType'), str):
            raise ValueError('미국 종목 유형을 확인하지 못했습니다.')
        if not row['isCommonShare'] or row['securityType'] not in STOCK_TYPES:
            continue
        if not isinstance(row.get('name'), str) or not isinstance(row.get('isinCode'), str) or not row['isinCode']:
            raise ValueError('미국 종목 식별 정보를 확인하지 못했습니다.')
        accepted.append(row)
    return accepted


class TossUSUniverse:
    """Use the collector's existing token and request budget; commit all exchanges together."""
    def __init__(self, repo, universe, *, clock=None):
        self.repo, self.universe = repo, universe
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.state = repo.get_control(STATE, {}) or {}

    def ready(self, now):
        bounds = selection_session(now)
        snapshot = self.universe.snapshot(now)
        return bool(bounds and self.state.get('complete') and self.state.get('session') == bounds[0].isoformat()
                    and snapshot and snapshot['source'] == SOURCE
                    and self.state.get('membership_hash') == symbols_digest(self.universe.members(now)))

    async def advance(self, read, *, limit=1):
        now = utc(self.clock())
        bounds = selection_session(now)
        if not bounds:
            return False
        session = bounds[0].isoformat()
        if (self.state.get('session') != session or not self.state.get('started_at')
                or (not self.state.get('complete') and now-utc(self.state['started_at']) > timedelta(minutes=15))):
            self.state = {'session': session, 'started_at': now.isoformat(), 'pages': {}, 'complete': False}
        if self.ready(now):
            return True
        for _ in range(limit):
            pending = [market for market in MARKETS if market not in self.state['pages']]
            if not pending:
                break
            market = pending[0]
            rows = await read('stocks_all', market)
            if rows is None:
                break
            catalog_page(rows)  # Do not cache malformed pages and repeatedly fail on the same bytes.
            self.state['pages'][market] = {'stocks': rows, 'received_at': utc(self.clock()).isoformat()}
            self.repo.set_control(STATE, self.state)
        finished = utc(self.clock())
        if selection_session(finished) != bounds:
            return False
        if set(self.state['pages']) != set(MARKETS):
            return False
        try:
            rows = catalog_members(self.state['pages'])
        except ValueError:
            self.state.update(pages={}, complete=False)
            self.repo.set_control(STATE, self.state)
            raise
        # Rehydrate a missing local cache after restart with the original known time.
        known = utc(self.state.get('completed_at') or finished)
        if (not utc(self.state['started_at']) <= known <= finished
                or any(not utc(self.state['started_at']) <= utc(p['received_at']) <= known for p in self.state['pages'].values())):
            raise ValueError('미국 목록 수집 시각이 잘못됐습니다.')
        self.universe.refresh(rows, source=SOURCE, observed_at=known)
        self.repo.record('universe_history', self.universe.snapshot(finished))
        self.state.update(complete=True, completed_at=known.isoformat(), membership_hash=symbols_digest(r['symbol'] for r in rows))
        self.repo.set_control(STATE, self.state)
        return True
