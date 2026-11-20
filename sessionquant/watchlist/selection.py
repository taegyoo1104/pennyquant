"""시총 후보·분봉 준비 운영 목록·집중 분석·보유 위험 관리를 분리한다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import asyncio
import json
from hashlib import sha256
from functools import wraps

from ..portfolio.models import decimal
from .capitalization import utc, ATTEMPT, FileMarketCapProvider, WatchlistSelector, WatchlistStore

STATE = 'watchlist_scan_state_v1'
VIEW = 'watchlist_dashboard_v1'


def snapshot_reads(method):
    """Share cap inputs only within a synchronous manager operation."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.read_scope():
            return method(self, *args, **kwargs)
    return wrapped


class FairScheduler:
    """실제로 시도한 시각이 가장 오래된 티커를 우선하며 재시작해도 순서를 복구한다."""
    def __init__(self, state):
        self.state = state
        self.last = state.setdefault('last_detail_attempt', {})

    def choose(self, symbols, count):
        return sorted(dict.fromkeys(symbols), key=lambda s: (self.last.get(s, ''), s))[:count]

    def mark(self, symbol, now):
        self.last[symbol] = utc(now).isoformat()


class FocusSelector:
    def __init__(self, size=20, max_age_seconds=120, minimum_hold_seconds=300, refresh_grace_seconds=120, method='activity'):
        self.size, self.age, self.hold = size, max_age_seconds, minimum_hold_seconds
        self.refresh_grace = refresh_grace_seconds
        self.method=method

    def choose(self, metrics, previous, now, *, pinned_symbols=()):
        """관찰 점수는 주문 허가가 아니다. RVOL·계좌 적격성을 계산하거나 대체하지 않는다."""
        eligible = [row for row in metrics if row.get('data_status') == 'VERIFIED'
                    and 0 <= (now-utc(row['known_at'])).total_seconds() <= self.age]
        eligible.sort(key=lambda r: (-Decimal(r['dollar_volume_usd']), r['ticker']))
        liquidity = {r['ticker']: Decimal(len(eligible)-i)/max(1, len(eligible)) for i, r in enumerate(eligible)}
        for row in eligible:
            if self.method=='turnover':
                row['focus_score']=str(liquidity[row['ticker']]*100)
                row['selection_reason']='후보 내 거래대금 순위 · 당일 분봉 수집·매수 조건은 별도 검사'
                continue
            relative = max(Decimal('-0.02'), min(Decimal('0.02'), Decimal(row['relative_strength_sample'])))
            volatility = min(Decimal('0.02'), abs(Decimal(row['sample_return'])))
            score = liquidity[row['ticker']]*60+(relative+Decimal('.02'))/Decimal('.04')*30+volatility/Decimal('.02')*10
            row['focus_score'] = str(score)
            row['selection_reason'] = '확인된 거래대금·관측 구간 상대강도·가격 변동; 주문 적격성은 별도 검사'
        eligible.sort(key=lambda r: (-Decimal(r['focus_score']), r['ticker']))
        by_symbol = {row['ticker']: row for row in eligible}
        metrics_by_symbol = {row['ticker']: row for row in metrics}
        pinned = []
        for row in previous:
            if row['ticker'] in by_symbol and (row['ticker'] in pinned_symbols or
                    0 <= (now-utc(row['focus_since'])).total_seconds() < self.hold):
                pinned.append({**by_symbol.pop(row['ticker']), 'focus_since': row['focus_since']})
            elif row['ticker'] not in by_symbol and self.refresh_grace:
                # Keep collection membership briefly while refreshing inputs.
                # No previous score or validity interval becomes entry evidence.
                metric = metrics_by_symbol.get(row['ticker'])
                if not metric or metric.get('data_status') != 'UNKNOWN':
                    continue
                try:
                    wait_since = utc(row.get('refresh_wait_since') or now)
                    deadline = min(utc(row.get('refresh_wait_until') or now+timedelta(seconds=self.refresh_grace)),
                                   utc(row['valid_until'])+timedelta(seconds=self.refresh_grace))
                    if not utc(row['focus_since']) <= wait_since <= now < deadline:
                        continue
                except (TypeError, ValueError, KeyError):
                    continue
                pinned.append({**row, 'data_status':'UNKNOWN',
                    'missing_inputs':metric.get('missing_inputs',[]), 'reason':metric['reason'],
                    'selection_reason':'자료 갱신 대기 · 신규 매수 보류',
                    'refresh_wait_since':wait_since.isoformat(),
                    'refresh_wait_until':deadline.isoformat()})
        pinned.sort(key=lambda r: (-Decimal(r['focus_score']), r['ticker']))
        result = pinned[:self.size]
        for row in eligible:
            if len(result) >= self.size:
                break
            if row['ticker'] not in by_symbol:
                continue
            old = next((r for r in previous if r['ticker'] == row['ticker']), None)
            result.append({**row, 'focus_since': old['focus_since'] if old else now.isoformat()})
        for row in result:
            row['monitor_until'] = (row.get('refresh_wait_until') or
                (utc(row['valid_until'])+timedelta(seconds=self.refresh_grace)).isoformat())
        return result


class WatchlistManager:
    def __init__(self, repo, universe, config, *, provider=None, clock=None, allow_synthetic=False,
                 history_selection=None):
        self.repo, self.universe, self.config = repo, universe, config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.allow_synthetic = allow_synthetic
        self.history_selection = history_selection
        self.pattern_provider = None
        self.provider_explicit = provider is not None
        self.provider = provider or FileMarketCapProvider(config.market_cap_input)
        self.selector = WatchlistSelector(config.watchlist_size, config.watchlist_refresh_days,
                                          missing_cap_policy=config.watchlist_missing_cap_policy)
        from .us_universe import POLICY
        self.store = WatchlistStore(repo, universe, max_age_days=config.watchlist_refresh_days,
                                   missing_cap_policy=config.watchlist_missing_cap_policy,
                                   required_selection_policy=POLICY if config.universe_source=='toss_us' else None)
        self.state = repo.get_control(STATE, {})
        self.state.setdefault('prices', {})
        self.state.setdefault('rankings', {})
        self.state.setdefault('focus', [])
        self.scheduler = FairScheduler(self.state)
        self.focus_selector = FocusSelector(config.focus_size, config.watchlist_data_max_age_seconds,
                                             config.focus_min_hold_seconds, config.focus_refresh_grace_seconds, config.focus_selection)

    def read_scope(self):
        return self.store.read_scope()

    async def refresh(self, now=None, *, force=False):
        now = utc(now or self.clock())
        attempt = self.repo.get_control(ATTEMPT, {})
        snapshot = self.store.at(now)
        if self.config.universe_source == 'toss_us':
            from .us_universe import selection_session, POLICY
            bounds = selection_session(now)
            status = self.store.status(now, allow_synthetic=self.allow_synthetic)
            if (bounds and status['status']=='VALID' and snapshot.get('selection_policy')==POLICY
                    and snapshot.get('session_open_at')==bounds[0].isoformat()):
                return status  # Even a manual force refresh cannot churn today's selected candidates.
        size_changed = bool(snapshot and snapshot.get('size_limit',len(snapshot.get('items',[]))) != self.config.watchlist_size)
        policy_changed = bool(snapshot and snapshot.get('missing_cap_policy','block') != self.config.watchlist_missing_cap_policy
                              and attempt.get('requested_missing_cap_policy') != self.config.watchlist_missing_cap_policy)
        if not force and not size_changed and not policy_changed and attempt.get('next_attempt_at') and now < utc(attempt['next_attempt_at']):
            return self.store.status(now, allow_synthetic=self.allow_synthetic)
        try:
            batch = await asyncio.wait_for(self.provider.fetch(), timeout=5)
            snapshot = self.selector.select(batch, self.universe, now, allow_synthetic=self.allow_synthetic)
            previous = self.store.at(now)
            # 불완전한 새 입력으로 기존 검증 이력을 대체하지 않는다.
            if snapshot.payload['data_status'] == 'VALID' or previous is None:
                self.store.add(snapshot)
            valid = snapshot.payload['data_status'] == 'VALID'
            report = {'status': 'ok' if valid else 'partial', 'attempted_at': now.isoformat(),
                'next_attempt_at': (min(now+timedelta(days=self.config.watchlist_refresh_days), utc(snapshot.payload.get('valid_until', (utc(snapshot.payload['as_of_at'])+timedelta(days=self.config.watchlist_refresh_days)).isoformat()))) if valid
                                    else now+timedelta(seconds=60)).isoformat(),
                'issues': snapshot.payload['issues'],
                'excluded_market_cap_symbols':snapshot.payload.get('excluded_market_cap_symbols',[]),
                'ranking_verified':snapshot.payload.get('ranking_verified',valid),
                'message': ('시총 미확인 종목 제외: 확인된 시총 범위에서 후보 선정' if snapshot.payload.get('excluded_market_cap_symbols')
                            else '시총 감시목록 갱신') if valid else '시총 자료 검증 미완료: 신규 진입 보류'}
        except (OSError, ValueError, TypeError, KeyError, httpx.HTTPError, RuntimeError) as exc:
            report = {'status': 'error', 'attempted_at': now.isoformat(),
                'next_attempt_at': (now+timedelta(seconds=60)).isoformat(), 'error_type': type(exc).__name__,
                'message': '검증된 시총 공급 자료가 없습니다. 기존 목록의 만료 여부를 확인하며 신규 임의 목록을 생성하지 않습니다.'}
            from .toss_capitalization import CollectionPending
            if isinstance(exc,CollectionPending):
                report.update(status='collecting',message=str(exc),next_attempt_at=now.isoformat())
            if isinstance(exc, httpx.HTTPStatusError):
                report['http_status'] = exc.response.status_code
                if exc.response.status_code == 429:
                    from .budget import provider_retry_until
                    report['next_attempt_at'] = provider_retry_until(exc.response.headers.get('Retry-After'),now).isoformat()
        report['requested_missing_cap_policy'] = self.config.watchlist_missing_cap_policy
        self.repo.set_control(ATTEMPT, report)
        self.publish(now)
        return self.store.status(now, allow_synthetic=self.allow_synthetic)

    @snapshot_reads
    def base_symbols(self, now):
        status = self.store.status(now, allow_synthetic=self.allow_synthetic)
        members = self.universe.members(now)
        if not status['snapshot'] or status['status'] == 'SYNTHETIC_BLOCKED':
            return []
        if self.config.universe_source == 'toss_us' and status['status'] != 'VALID':
            return []
        from ..market_reference.contracts import REFERENCE_SYMBOLS
        return [row['ticker'] for row in status['items'] if row['ticker'] in members and row['ticker'] not in REFERENCE_SYMBOLS][:self.config.watchlist_size]

    @snapshot_reads
    def light_symbols(self, now):
        status = self.store.status(now, allow_synthetic=self.allow_synthetic)
        if not status['snapshot'] or status['snapshot']['data_status'] != 'VALID':
            return []
        symbols = self.base_symbols(now)
        return self.history_selection.allowed(symbols, now) if self.history_selection else symbols

    def observe_prices(self, rows, received_at):
        received_at = utc(received_at)
        if not isinstance(rows, list):
            raise ValueError('현재가 일괄 응답이 배열이 아닙니다.')
        seen = set()
        allowed = set(self.light_symbols(received_at)) | {'SPY', 'QQQ'}
        for row in rows:
            symbol = row.get('symbol') if isinstance(row, dict) else None
            if symbol not in allowed:
                continue
            if symbol in seen:
                raise ValueError('일괄 현재가의 티커 중복입니다.')
            seen.add(symbol)
            try:
                stamp = utc(row['timestamp'])
                value = decimal(row['lastPrice'], positive=True)
                if row['currency'] != 'USD' or not stamp <= received_at:
                    continue
                current = {'price': str(value), 'as_of_at': stamp.isoformat(), 'known_at': received_at.isoformat()}
                old = self.state['prices'].get(symbol, [])
                if old and stamp <= utc(old[0]['as_of_at']):
                    continue
                self.state['prices'][symbol] = [current]+old[:1]
            except (ValueError, TypeError, KeyError):
                continue

    def observe_rankings(self, response, received_at):
        received_at = utc(received_at)
        if not isinstance(response, dict) or not isinstance(response.get('rankings'), list):
            raise ValueError('거래대금 랭킹 응답 형식을 확인할 수 없습니다.')
        if not response['rankings']:
            self.state['rankings'] = {}
            return
        stamp = utc(response['rankedAt'])
        if stamp > received_at:
            raise ValueError('미래 랭킹을 사용할 수 없습니다.')
        allowed = set(self.light_symbols(received_at))
        fresh = {}
        for row in response['rankings']:
            try:
                symbol = row['symbol']
                if symbol not in allowed or row['currency'] != 'USD':
                    continue
                if symbol in fresh:
                    raise ValueError('랭킹 티커 중복')
                amount = decimal(row['tradingAmount'], positive=True)
                volume = decimal(row['tradingVolume'], positive=True)
                fresh[symbol] = {'as_of_at': stamp.isoformat(), 'known_at': received_at.isoformat(),
                    'dollar_volume_usd': str(amount), 'volume': str(volume), 'source': 'toss:MARKET_TRADING_AMOUNT:1d'}
                # RankingPrice.basePrice is the previous-day base for MARKET_*
                # rankings (TOP_GAINERS/LOSERS instead use the period start).
                # Optional display evidence must never change focus eligibility.
                price = row.get('price')
                if isinstance(price, dict):
                    try:
                        fresh[symbol]['previous_close'] = str(decimal(price.get('basePrice'), positive=True))
                    except ValueError:
                        pass
            except (TypeError, KeyError):
                continue
        self.state['rankings'] = fresh

    def _metric(self, symbol, now):
        empty = {'ticker': symbol, 'data_status': 'UNKNOWN', 'reason': '현재가 이력·거래대금·시장 비교 자료 미확인',
                 'missing_inputs': [],
                 'rvol': None, 'sector_strength': None, 'tradable': None, 'fractional': None}
        current = self.state['prices'].get(symbol, [])
        spy = self.state['prices'].get('SPY', [])
        ranking = self.state['rankings'].get(symbol)
        if self.config.focus_selection=='turnover':
            if not current or not ranking:
                return {**empty,'missing_inputs':['price_or_turnover_missing'],'reason':'현재가·거래대금 수신 대기'}
            points=[current[0],ranking]
            age=self.config.watchlist_data_max_age_seconds
            if any(not 0<=(now-utc(r['as_of_at'])).total_seconds()<=age or utc(r['known_at'])>now for r in points):
                return {**empty,'missing_inputs':['observation_stale_or_future'],'reason':'현재가·거래대금 갱신 대기'}
            return {**empty,**ranking,'data_status':'VERIFIED','reason':'최신 현재가·거래대금 확인',
                'known_at':current[0]['known_at'],'as_of_at':current[0]['as_of_at'],
                'metric_basis':'turnover_only','sample_return':None,'relative_strength_sample':None,
                'valid_until':min(utc(r['as_of_at'])+timedelta(seconds=age) for r in points).isoformat()}
        if len(current) < 2 or len(spy) < 2 or not ranking:
            missing, labels = [], []
            for values, prefix, label in ((current,'symbol','종목'),(spy,'spy','시장(SPY)')):
                if not values:
                    missing.append(prefix+'_prices_missing'); labels.append(label+' 현재가 미수신')
                elif len(values) < 2:
                    missing.append(prefix+'_second_price_missing'); labels.append(label+' 비교 가격 1회 부족')
            if not ranking:
                missing.append('turnover_missing'); labels.append('거래대금 미확인')
            return {**empty, 'missing_inputs':missing, 'reason':' · '.join(labels)}
        age = self.config.watchlist_data_max_age_seconds
        points = [*current, *spy, ranking]
        if any(not 0 <= (now-utc(row['as_of_at'])).total_seconds() <= age
               or utc(row['known_at']) > now for row in points):
            return {**empty, 'missing_inputs':['observation_stale_or_future'], 'reason': '관찰 자료 지연·미래 시각'}
        # 서로 다른 길이의 관측 구간을 같은 상대강도 지표로 합치지 않는다.
        if any(abs((utc(current[i]['known_at'])-utc(spy[i]['known_at'])).total_seconds()) > 1 for i in (0, 1)):
            return {**empty, 'missing_inputs':['observation_window_mismatch'], 'reason': '종목·시장 관측 구간 불일치'}
        change = Decimal(current[0]['price'])/Decimal(current[1]['price'])-1
        market = Decimal(spy[0]['price'])/Decimal(spy[1]['price'])-1
        return {**empty, 'data_status': 'VERIFIED', 'reason': '관측 구간 가격·거래량·거래대금 확인',
            'known_at': current[0]['known_at'], 'as_of_at': current[0]['as_of_at'],
            'sample_return': str(change), 'relative_strength_sample': str(change-market),
            'metric_basis': 'same_observation_window_vs_SPY', **ranking,
            'valid_until': min(utc(row['as_of_at'])+timedelta(seconds=age) for row in points).isoformat()}

    @snapshot_reads
    def focus(self, now):
        now = utc(now)
        if self.store.status(now, allow_synthetic=self.allow_synthetic)['status'] != 'VALID':
            self.state['focus'] = []
            return []
        metrics = [self._metric(s, now) for s in self.light_symbols(now)]
        if self.config.focus_rebalance_minutes:
            from .focus_schedule import choose_scheduled
            changed = choose_scheduled(self, metrics, now)
            if changed:self.repo.set_control(STATE,self.state)
        else:
            self.state['focus'] = self.focus_selector.choose(metrics, self.state['focus'], now)
        return [row['ticker'] for row in self.state['focus']]

    @snapshot_reads
    def entry_blockers(self, symbol, now):
        now = utc(now)
        from ..market_reference.contracts import REFERENCE_SYMBOLS
        if symbol in REFERENCE_SYMBOLS:return ['market_reference_not_trading_candidate']
        status = self.store.status(now, allow_synthetic=self.allow_synthetic)
        if status['status'] != 'VALID':
            return ['watchlist_'+status['status'].lower()]
        if symbol not in set(self.base_symbols(now)):
            return ['outside_market_cap_watchlist']
        if self.history_selection and self.history_selection.startup_pending:
            return ['startup_data_not_ready']
        if symbol not in set(self.light_symbols(now)):
            return ['history_not_in_daily_operating_watchlist']
        row = next((r for r in self.state['focus'] if r['ticker'] == symbol), None)
        if row and row.get('refresh_wait_until'):
            return ['focus_refresh_pending']
        if not row or self._metric(symbol, now)['data_status'] != 'VERIFIED':
            return ['focus_unknown_or_stale']
        if self.state.get('backoff_until') and now < utc(self.state['backoff_until']):
            return ['market_provider_backoff']
        return []

    def candidate_symbols(self,now):
        """동시에 비교할 집중 목록을 반환한다. 각 종목의 진입 적격성은 별도 검사한다."""
        return self.focus(utc(now))

    def candidate_expiry(self,symbol,now):
        row=next((item for item in self.state['focus'] if item['ticker']==symbol),None)
        status=self.store.status(utc(now),allow_synthetic=self.allow_synthetic)
        snapshot=status.get('snapshot') or {}
        if not row or not snapshot:return None
        if row.get('data_status') != 'VERIFIED':return utc(now).isoformat()
        expires=[utc(row['valid_until']),utc(snapshot['as_of_at'])+timedelta(days=self.config.watchlist_refresh_days)]
        if snapshot.get('valid_until'):expires.append(utc(snapshot['valid_until']))
        return min(expires).isoformat()

    @snapshot_reads
    def news_targets(self, now, priority_symbols=()):
        """단계 06용 계약: 기업 후보 상한과 위험 우선대상, 거시 수집의 독립성을 제공한다."""
        now = utc(now)
        status = self.store.status(now, allow_synthetic=self.allow_synthetic)
        allowlist = self.light_symbols(now) if status['status'] == 'VALID' else []
        focus = [s for s in self.focus(now) if s in allowlist]
        risk = list(dict.fromkeys(s for s in priority_symbols if s not in {'SPY', 'QQQ'}))
        return {'company_allowlist': allowlist, 'focus_candidates': focus, 'risk_priority': risk,
                'analysis_symbols': list(dict.fromkeys([*risk, *focus])), 'macro_independent': True}

    @snapshot_reads
    def publish(self, now, *, priority_symbols=()):
        now = utc(now)
        self.focus(now)
        contract = self.news_targets(now, priority_symbols)
        status = self.store.status(now, allow_synthetic=self.allow_synthetic)
        base = self.base_symbols(now)
        history = self.history_selection.ensure(base, now) if self.history_selection and base else {}
        included = set(history.get('included_symbols', base))
        operating = set(self.light_symbols(now))
        history_rows = {r['symbol']:r for r in history.get('items', [])}
        focused = {row['ticker']: row for row in self.state['focus']}
        rows = []
        for row in status['items'][:self.config.watchlist_size]:
            symbol = row['ticker']
            metric = self._metric(symbol, now)
            rows.append({**row, 'history_ready':symbol in included,
                'operating':symbol in operating, 'history_selection':history_rows.get(symbol),
                'focused': symbol in focused, 'focus_reason': focused.get(symbol, {}).get('selection_reason', metric['reason']),
                'focus_missing_inputs':metric['missing_inputs'],
                'last_detail_at': self.state.get('last_detail_success', {}).get(symbol),
                'focus_valid_until':focused.get(symbol,{}).get('valid_until'),
                'focus_monitor_until':focused.get(symbol,{}).get('monitor_until'),
                'focus_since':focused.get(symbol,{}).get('focus_since'),
                'focus_pattern_retained':focused.get(symbol,{}).get('pattern_retained',False),
                'focus_refresh_pending':bool(focused.get(symbol,{}).get('refresh_wait_until')),
                'last_detail_attempt_at': self.scheduler.last.get(symbol), 'light_data_status': metric['data_status'],
                'last_price_at': self.state['prices'].get(symbol, [{}])[0].get('as_of_at'),
                'last_price': self.state['prices'].get(symbol, [{}])[0].get('price'),
                'day_price_base': {key: self.state['rankings'].get(symbol, {}).get(key)
                    for key in ('previous_close', 'as_of_at', 'known_at', 'source')},
                'entry_blockers': self.entry_blockers(symbol, now)})
        previous = self.state.get('focus_recorded_at')
        identity = sorted(focused)
        evidence_hash = sha256(json.dumps(self.state['focus'],sort_keys=True).encode()).hexdigest()
        watchlist_id = status['snapshot']['id'] if status['snapshot'] else None
        if (previous is None or (now-utc(previous)).total_seconds() >= 60
                or evidence_hash != self.state.get('recorded_focus_evidence')
                or watchlist_id != self.state.get('recorded_watchlist_id')):
            valid_until = min((row['valid_until'] for row in self.state['focus']
                               if row.get('data_status') == 'VERIFIED'), default=now.isoformat())
            self.repo.record('pipeline_status', {'id':'watchlist-focus:'+now.isoformat(), 'kind':'watchlist_focus',
                'known_at':now.isoformat(), 'valid_until':valid_until,
                'watchlist_id': status['snapshot']['id'] if status['snapshot'] else None,
                'synthetic': status['snapshot']['synthetic'] if status['snapshot'] else False,
                'data_status': status['status'], 'items':self.state['focus'],
                'history_selection_checked_at':history.get('checked_at')})
            self.state['focus_recorded_at'] = now.isoformat()
            self.state['recorded_focus_symbols'] = identity
            self.state['recorded_focus_evidence'] = evidence_hash
            self.state['recorded_watchlist_id'] = watchlist_id
        self.repo.set_control(STATE, self.state)
        self.repo.set_control(VIEW, {**status, 'updated_at': now.isoformat(), 'items': rows,
            'universe_source': self.config.universe_source,
            'watchlist_limit': self.config.watchlist_size, 'focus_limit': self.config.focus_size,
            'focus_schedule':self.state.get('focus_schedule',{}),
            'history_selection':history, 'operating_count':len(operating),
            'data_max_age_seconds':self.config.watchlist_data_max_age_seconds, 'max_age_days':self.config.watchlist_refresh_days,
            'focus_count': len(focused), 'source_state': {k: v for k, v in self.state.items() if k not in {'prices', 'rankings'}},
            'news_contract': contract})


class HistoricalWatchlistPolicy:
    """저장된 선택 기록만 과거 재생에 적용한다. 최신 RSS 목록을 참조하지 않는다."""
    def __init__(self, history, universe, *, allow_synthetic=False, max_age_days=7):
        if history.get('schema_version') != 1:
            raise ValueError('감시목록 이력 형식이 잘못되었습니다.')
        self.history, self.universe = history, universe
        self.allow_synthetic, self.max_age = allow_synthetic, timedelta(days=max_age_days)

    def entry_blockers(self, symbol, now):
        from .capitalization import symbols_digest
        now = utc(now)
        snapshots = [r for r in self.history.get('snapshots', []) if utc(r['known_at']) <= now and utc(r['as_of_at']) <= now]
        if not snapshots:
            return ['watchlist_history_unknown']
        cap = max(snapshots, key=lambda r:r['known_at'])
        if (cap['data_status'] != 'VALID' or now-utc(cap['as_of_at']) > self.max_age
                or (cap.get('valid_until') and now>=utc(cap['valid_until']))):
            return ['watchlist_history_expired_or_partial']
        if cap['synthetic'] and not self.allow_synthetic:
            return ['watchlist_history_synthetic_blocked']
        if symbols_digest(self.universe.members(now)) != cap['membership_hash']:
            return ['watchlist_history_membership_unknown']
        if symbol not in {r['ticker'] for r in cap['items']}:
            return ['outside_market_cap_watchlist']
        frames = [r for r in self.history.get('focus_history', []) if utc(r['known_at']) <= now]
        frame = max(frames, key=lambda r:r['known_at']) if frames else None
        if (not frame or not frame.get('items') or frame.get('watchlist_id') != cap['id'] or frame.get('data_status') != 'VALID'
                or utc(frame['valid_until']) < now or frame.get('synthetic') != cap['synthetic']):
            return ['focus_history_unknown_or_stale']
        if symbol not in {r['ticker'] for r in frame['items']}:
            return ['outside_historical_focus']
        row = next(r for r in frame['items'] if r['ticker'] == symbol)
        if row.get('data_status') != 'VERIFIED' or utc(row['valid_until']) < now:
            return ['focus_history_unknown_or_stale']
        return []
