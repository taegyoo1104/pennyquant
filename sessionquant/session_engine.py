"""시장·뉴스 위험·소수점 모의 주문을 연결하는 단일 작성자 엔진.

뉴스 API는 이 모듈에서 호출하지 않는다. 저장 실패 후에는 같은 인스턴스로
주문을 계속하지 않으며, 재시작 시 커밋된 장부와 수신 커서부터 복구한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace,asdict
from hashlib import sha256
from contextlib import nullcontext
import json

from .database.repository import Repository
from .events import parse_event
from .market.bars import indicators, contiguous
from .market.engine import MarketEngine
from .market.scanner import InstrumentEligibility
from .market.universe import UniverseStore
from .models import jsonable, utc
from .portfolio.broker import PaperBroker
from .portfolio.manager import PortfolioManager
from .portfolio.models import PortfolioSettings, serialize
from .portfolio.session import SessionExitManager
from .portfolio.exit_policy import ExitPolicy
from .performance import timed


def event_time(raw: dict) -> datetime:
    """외부 이벤트가 실제 사용 가능해진 시각을 읽는다."""
    data = raw["data"]
    field = {"eligibility": "checked_at", "rvol": "observed_at", "risk_context": "available_at"}.get(raw["kind"], "received_at")
    received = utc(data[field])
    if 'restored_at' in raw:
        restored = utc(raw['restored_at'])
        if raw['kind'] != 'bar' or restored < received:
            raise ValueError('저장 분봉 복원 시각은 원본 수신 이후여야 합니다.')
        received = restored
    if 'delivered_at' in raw:
        delivered = utc(raw['delivered_at'])
        if raw['kind'] not in {'bar', 'eligibility', 'quote'} or delivered < received:
            raise ValueError('지연 자료 전달 시각은 원본 수신·복원 이후여야 합니다.')
        received = delivered
    return received


class SessionEngine:
    def __init__(self, repository: Repository, universe: UniverseStore, *, strategy: str = "pullback",
                 settings: PortfolioSettings | None = None, restore: bool = False, risk_provider=None,
                 synthetic: bool = False, use_news: bool = True, use_market_filter: bool = True, control_state: dict | None = None, watchlist_policy=None,
                 strict_market_data: bool = False, exit_policy: ExitPolicy | None = None, macro_reference_risk=None, record_inputs=False, research_replay=False,
                 candidate_settings=None, paper_data_policy='strict', quote_entry_guard=None,
                 benchmark_collection_enabled=None,market_reference_symbols=None,execution_clock=None,quote_recovery_wait=False,
                 submission_quote_buys=False,submission_quote_sells=False):
        from .portfolio.selection import PortfolioCandidateSelector,CandidateSettings
        self.candidate_selector=PortfolioCandidateSelector(CandidateSettings(enabled=False) if candidate_settings is None else candidate_settings)
        self.quote_entry_guard=quote_entry_guard
        self.quote_entry_evidence=None
        if type(quote_recovery_wait) is not bool:
            raise ValueError('호가 복구 대기 설정은 불리언이어야 합니다.')
        self.quote_recovery_wait=quote_recovery_wait
        if type(submission_quote_buys) is not bool:
            raise ValueError('주문 시점 호가 체결 설정은 불리언이어야 합니다.')
        if submission_quote_buys and settings is not None and settings.paper_fill_model!='market_v2':
            raise ValueError('주문 시점 호가 체결은 market_v2 모의 장부에서만 사용할 수 있습니다.')
        self.submission_quote_buys=submission_quote_buys
        if type(submission_quote_sells) is not bool:
            raise ValueError('매도 주문 시점 호가 체결 설정은 불리언이어야 합니다.')
        if submission_quote_sells and settings is not None and settings.paper_fill_model!='market_v2':
            raise ValueError('주문 시점 호가 체결은 market_v2 모의 장부에서만 사용할 수 있습니다.')
        self.submission_quote_sells=submission_quote_sells
        self.selection_frame=None
        self.selection_source_id=None
        self._batch_candidate_scope=[]
        self.record_inputs = record_inputs
        self._batch_decisions = []
        self._batch_news_contexts = {}
        self._batch_scope_blockers = None
        self.macro_reference_risk = macro_reference_risk
        self.reference_risk_status = {}
        if macro_reference_risk is not None and macro_reference_risk.effective_enabled and not synthetic and not research_replay:
            raise ValueError('위험 필터 ON 정책은 격리 합성 연구 또는 승인된 오프라인 기록 재생에서만 검증할 수 있습니다.')
        self.control_state=control_state
        self.watchlist_policy=watchlist_policy
        self.repo, self.universe = repository, universe
        self.strategy, self.synthetic, self.use_news = strategy, synthetic, use_news
        self.use_market_filter = use_market_filter
        # Display metadata only. Unknown/older runs must not hide missing inputs.
        self.benchmark_collection_enabled = benchmark_collection_enabled
        self.market_reference_symbols = market_reference_symbols
        self.market = MarketEngine(universe, strategy, use_market_filter=use_market_filter, strict_data=strict_market_data,
                                   paper_data_policy=paper_data_policy)
        if strategy=='pullback_simple' and hasattr(watchlist_policy,'pattern_provider'):
            from .market.simple_pullback import progress
            watchlist_policy.pattern_provider=lambda symbol,now:progress(indicators(self.market.bars.get(symbol,[]),now))
        self.broker = PaperBroker(settings or PortfolioSettings())
        if paper_data_policy == 'observed_paper_v1' and self.broker.settings.paper_fill_model != 'market_v2':
            raise ValueError('실데이터 Paper 정책은 market_v2 체결 모형이 필요합니다.')
        self.failed = False
        self.risk_provider = risk_provider
        self.last_time = None
        self.last_decision_at = None
        self.cursor = 0
        self.risk_records: dict[str, dict] = {}
        self.risk_provider_cache = {}
        self._risk_lookup_errors = set()
        self.exit_policy = exit_policy or ExitPolicy()
        self.consumed_signals: set[str] = set()
        self.dirty_symbols: set[str] = set()
        self.dirty_bar_symbols: set[str] = set()
        self._market_storage_version = 3
        self.market_symbols: set[str] = set()
        self._journal_hashes: dict[str, str] = {}
        self._last_summary = None
        self._last_persist_at = None
        stored = self.repo.load_state()
        if stored is not None:
            if not restore:
                raise ValueError("이미 저장된 상태가 있습니다. 복구 옵션 또는 별도 DB를 사용하세요.")
            if (stored.get("schema_version") not in (2, 3) or stored.get("environment") != "PAPER_LOCAL"
                    or (stored.get('strict_market_data',False) and not strict_market_data)
                    or stored.get("strategy") != strategy or stored.get("synthetic") != synthetic
                    or stored.get("use_news", True) != use_news
                    or stored.get('paper_data_policy','strict') != paper_data_policy
                    or stored.get("use_market_filter", True) != use_market_filter):
                raise ValueError("저장된 실행 환경·전략·자료 유형이 다릅니다. 기존 기록을 덮어쓰지 않습니다.")
            self.broker = PaperBroker.from_state(stored["broker"], settings)
            if stored.get('candidate_selection') is not None and stored['candidate_selection']!=asdict(self.candidate_selector.settings):
                raise ValueError('후보 선정 정책이 저장된 실행과 다릅니다. 별도 DB를 사용하세요.')
            self.last_time = utc(stored["last_time"]) if stored.get("last_time") else None
            clocks=[value for value in (self.last_time,self.broker.last_event_at,
                utc(stored['last_decision_at']) if stored.get('last_decision_at') else None) if value is not None]
            self.last_decision_at=max(clocks) if clocks else None
            self.cursor = stored.get("cursor", 0)
            self.consumed_signals = set(stored.get("consumed_signals", []))
            self.risk_records = stored.get("risk_records", {})
            if ExitPolicy(**stored.get('exit_policy',{})) != self.exit_policy:
                raise ValueError('손절·청산 연구 정책이 저장된 실행과 다릅니다. 새 DB를 사용하세요.')
            self.risk_provider_cache = stored.get('risk_provider_cache',{})
            self.market_symbols = set(stored.get("market_symbols", []))
            self._market_storage_version = stored['schema_version']
            merged = {"schema_version": 1, **{name: {} for name in ("bars", "quotes", "quote_conflicts", "eligibility", "rvol", "sector_strength")}}
            for symbol in sorted(self.market_symbols):
                chunk = self.repo.get_control("sq_market_" + symbol)
                if not chunk:
                    raise ValueError("시장 복구 자료가 누락되어 신규 주문을 중단합니다.")
                if self._market_storage_version == 3:
                    metadata = self.repo.get_control("sq_market_meta_" + symbol)
                    if not metadata or metadata.get('schema_version') != 1:
                        raise ValueError("시장 메타데이터 복구 자료가 누락되어 신규 주문을 중단합니다.")
                    chunk = {**chunk, **{name: metadata.get(name, {}) for name in
                             ('quotes','quote_conflicts','eligibility','rvol','sector_strength')}}
                for name in merged:
                    if name != "schema_version":
                        merged[name].update(chunk.get(name, {}))
            self.market.restore(merged)
        elif self.repo.get_control("runtime_kind") not in (None, "sessionquant"):
            raise ValueError("기존 PennyQuant DB는 호환 실행 경로로 열거나 새 DB를 지정하세요.")
        self.manager = PortfolioManager(self.broker,self.exit_policy)
        self.exit_manager = SessionExitManager(self.broker)
        from .portfolio.execution_guard import BuyExecutionGuard, SellExecutionGuard
        if research_replay and execution_clock is not None:
            raise ValueError('기록 재생에는 실제 실행 시계를 사용할 수 없습니다.')
        self.execution_guard = BuyExecutionGuard(self.broker, execution_clock,
            last_checked_at=(stored or {}).get('last_execution_check_at'),
            eligibility_check=self._execution_eligibility_allowed)
        self.sell_execution_guard=SellExecutionGuard(self.broker,execution_clock,
            last_checked_at=(stored or {}).get('last_sell_execution_check_at'))
        self.manager.exit_submitter=self._submit_exit
        self.exit_manager.manager.exit_submitter=self._submit_exit
        # Display-only latest assessment; never an entry/exit input. Preserve its
        # original timestamp through heartbeat-only ticks and restarts.
        self._lab_rows=((self.repo.get_control('sessionquant_context',{}) or {}).get('strategy_lab',{}).get('rows',[])
                        if restore and strategy=='pullback_simple' else [])

    def risk_context(self, now: datetime, symbol: str) -> dict:
        context = self._risk_context(now,symbol)
        if self.strategy == 'pullback_simple':
            context = {**context, 'block_entries':False, 'major_adverse':False, 'blackout':False,
                       'force_liquidation':False, 'llm_role':'candidate_priority_only'}
        if self.record_inputs:
            try:
                evidence = self.risk_provider.recorded_evidence(now,symbol) if self.risk_provider and hasattr(self.risk_provider,'recorded_evidence') else {}
            except Exception as error:
                evidence = {'error_type':type(error).__name__,'analyses':[],'economic_events':[]}
            self._batch_news_contexts[symbol] = {'context':context,'evidence':evidence,'available_at':now.isoformat()}
        return context

    def _risk_context(self, now: datetime, symbol: str) -> dict:
        empty = {"status": "UNKNOWN", "block_entries": False, "major_adverse": False,
                 "blackout": False, "reasons": ["뉴스 상태 미확인"]}
        if not self.use_news:
            return empty | {"status": "DISABLED"}
        if self.risk_provider:
            try:
                context = self.risk_provider.risk_context(now,symbol=symbol)
                if not isinstance(context,dict) or any(type(context.get(key,False)) is not bool for key in ('block_entries','major_adverse','blackout')):
                    raise ValueError('뉴스 위험 상태 형식 미확인')
            except Exception as exc:
                if symbol not in self._risk_lookup_errors:
                    self.repo.log('WARNING','뉴스 위험 조회에 실패했습니다. 가격 손절·시간 청산을 계속하며 확인되지 않은 악재를 생성하지 않습니다.',
                                  error_type=type(exc).__name__,symbol=symbol)
                self._risk_lookup_errors.add(symbol)
                cached = self.risk_provider_cache.get(symbol,{})
                known = bool(cached.get('available_at') and utc(cached['available_at']) <= now)
                return empty|{'block_entries':bool(known and cached.get('block_entries')),
                    'blackout':bool(known and cached.get('blackout')),'provider_error':True,
                    'reasons':['뉴스 위험 조회 실패: UNKNOWN. 이전 신규 진입 제한은 조회 복구까지 유지합니다.']}
            self._risk_lookup_errors.discard(symbol)
            self.risk_provider_cache[symbol] = {'available_at':utc(now).isoformat(),
                'block_entries':context.get('block_entries',False),'blackout':context.get('blackout',False)}
            return context
        records = [self.risk_records.get(symbol), self.risk_records.get("*")]
        available = [r for r in records if r and r.get("verified") is True and
                     utc(r["available_at"]) <= now and 0 <= (now - utc(r["available_at"])).total_seconds() <= 86400]
        if not available:
            return empty
        return {"status": "KNOWN", "block_entries": any(r.get("block_entries") is True for r in available),
                "major_adverse": any(r.get("major_adverse") is True and r.get("scope") == "company" for r in available),
                "blackout": any(r.get("blackout") is True for r in available),
                "reasons": [reason for r in available for reason in r.get("reasons", [])]}

    def _bearish(self, symbol: str, now: datetime) -> bool:
        data = indicators(self.market.bars.get(symbol, []), now)
        recent = list(data.five[-3:])
        return bool(len(recent) == 3 and contiguous(recent, 5) and data.vwap and
                    (now - recent[-1].timestamp).total_seconds() <= 330 and
                    recent[-1].close < recent[-2].low and recent[-1].close < data.vwap and
                    recent[-1].volume >= recent[-2].volume)

    def _ingest(self, raw: dict):
        kind, data = raw["kind"], raw["data"]
        if kind in ("bar", "quote"):
            event = parse_event(raw)
            value = event.data
            if kind == "bar":
                self.market.on_bar(value)
                self.repo.record("market_bars", value)
                self.dirty_bar_symbols.add(value.symbol)
            else:
                self.market.on_quote(value)
                self.repo.record("quote_snapshots", value)
            self.dirty_symbols.add(value.symbol)
        elif kind == "eligibility":
            item = InstrumentEligibility(**{**data, "checked_at": utc(data["checked_at"])})
            self.market.set_eligibility(item.symbol, item)
            self.repo.record("stocks", {"id": "eligibility:" + item.symbol, **jsonable(item)})
            self.dirty_symbols.add(item.symbol)
        elif kind == "rvol":
            self.market.set_rvol(data["symbol"], data["value"], utc(data["observed_at"]), verified=data.get("verified", False))
            self.dirty_symbols.add(data["symbol"])
        elif kind == "risk_context":
            if data.get("scope") not in {"company", "macro"} or not isinstance(data.get("source"), str):
                raise ValueError("재생용 뉴스 위험 자료에는 범위와 출처가 필요합니다.")
            self.risk_records[data.get("symbol", "*")] = dict(data)
            self.repo.record("news_analysis" if data["scope"] == "company" else "macro_analysis", data)
        elif kind == "news":
            self.repo.record("news_events", parse_event(raw).data)
        else:
            raise ValueError(f"지원하지 않는 SessionQuant 이벤트: {kind}")

    def _record_changed(self, kind: str, identity: str, value):
        payload = jsonable(value)
        digest = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        key = kind + ":" + identity
        if self._journal_hashes.get(key) != digest:
            self.repo.record(kind, payload)
            self._journal_hashes[key] = digest

    @timed('engine_persist')
    def _persist(self, now: datetime, session_status: dict):
        risk_snapshot = self.manager.risk_snapshot(now)
        session_status['alerts'] = list(self.broker.alerts)
        # 종목별 시장 상태를 분리하여 매 호가마다 전체 유니버스를 다시 저장하지 않는다.
        metadata = self.market.to_state(include_bars=False)
        changed_symbols = self.dirty_symbols | (self.market_symbols if self._market_storage_version == 2 else set())
        for symbol in sorted(changed_symbols):
            payload = {"schema_version": 1}
            for name in ("quotes", "quote_conflicts", "eligibility", "rvol", "sector_strength"):
                payload[name] = {symbol: metadata[name][symbol]} if symbol in metadata[name] else {}
            # Quotes change much more often than a 20-session bar checkpoint.
            # Both writes and the manifest commit atomically in apply_batch.
            if symbol in self.dirty_bar_symbols or symbol not in self.market_symbols:
                self.repo.set_control("sq_market_" + symbol,
                    {"schema_version": 1, "bars": {symbol: jsonable(self.market.bars.get(symbol, []))}})
            self.repo.set_control("sq_market_meta_" + symbol, payload)
            self.market_symbols.add(symbol)
        self.dirty_symbols.clear()
        self.dirty_bar_symbols.clear()
        self._market_storage_version = 3
        broker_state = self.broker.to_state()
        # Old binaries reject version 3 rather than silently restoring stale quotes.
        state = {"schema_version": 3, "environment": "PAPER_LOCAL", "strategy": self.strategy,
                 'candidate_selection':asdict(self.candidate_selector.settings),
                 'risk_policy_version':2,'exit_policy':serialize(self.exit_policy),'risk_provider_cache':self.risk_provider_cache,
                 "strict_market_data":self.market.strict_data,
                 'paper_data_policy':self.market.paper_data_policy,
                 "synthetic": self.synthetic, "use_news": self.use_news, "use_market_filter": self.use_market_filter, "broker": broker_state,
                 "cursor": self.cursor, "last_time": self.last_time.isoformat() if self.last_time else None,
                 'last_decision_at':self.last_decision_at.isoformat() if self.last_decision_at else None,
                 'last_execution_check_at':self.execution_guard.last_checked_at.isoformat()
                     if self.execution_guard.last_checked_at else None,
                 **({'last_sell_execution_check_at':self.sell_execution_guard.last_checked_at.isoformat()}
                    if self.sell_execution_guard.last_checked_at else {}),
                 "market_symbols": sorted(self.market_symbols), "consumed_signals": sorted(self.consumed_signals),
                 "risk_records": self.risk_records}
        self.repo.save_state(state)
        for order in self.broker.orders.values():
            row = serialize(order)
            row.update(serialize(order.intent))
            row['expected_price'] = serialize(order.expected_price)
            self._record_changed("orders", order.client_id, row)
        for fill in self.broker.fills:
            row=serialize(fill)
            order=self.broker.orders.get(fill.client_id)
            timing_key=fill.side+'_fill_timing'
            if order and order._order_audit().get(timing_key)=='submission_quote':
                row[timing_key]='submission_quote' if fill.timestamp==order.submitted_at else 'next_quote'
            self._record_changed("executions", fill.id, row)
        self.repo.replace_positions(self.broker.positions)
        for alert in self.broker.alerts:
            self._record_changed("alerts", alert["id"], alert)
        account = serialize(self.broker.get_account())
        self.repo.record("session_state", {"id": self.broker.session_date, "timestamp": now, **serialize(session_status)})
        self.repo.record("daily_performance", {"id": self.broker.session_date, "timestamp": now,
            "equity": account["equity"], "day_start_equity": account["day_start_equity"],
            "realized_pnl": account["realized_pnl"], "halted": self.broker.halted})
        self.repo.record("account_snapshots", {"id": now.isoformat(), "timestamp": now, **account})
        self.repo.record("reconciliations", {"id": now.isoformat(), "timestamp": now, **self.broker.reconcile()})
        regime = self.market.regime(now).to_dict()
        self.repo.record("market_regimes", {"id": now.isoformat(), "timestamp": now, **regime})
        context = {"environment": "PAPER_LOCAL", "mode": "paper", "live_orders_enabled": False,
                   "synthetic": self.synthetic, "strategy": self.strategy, "updated_at": now.isoformat(),
                   'paper_data_policy':self.market.paper_data_policy,
                   'benchmark_collection_enabled':self.benchmark_collection_enabled,
                   'market_reference_symbols':self.market_reference_symbols,
                   "market_regime": regime, "session": serialize(session_status), "account": account,
                   "backend": self.repo.backend, "news_status": "UNKNOWN" if self.risk_provider is None else 'DEGRADED' if self._risk_lookup_errors else "ENABLED",
                   'risk_policy_version':2,'exit_policy':serialize(self.exit_policy),'risk_snapshot':risk_snapshot,
                   'macro_reference_risk':self.reference_risk_status,
                   'candidate_selection':asdict(self.candidate_selector.settings),
                   "paper_fill_model":self.broker.settings.paper_fill_model, "paper_ledger_version":2,
                   'quote_recovery_wait':self.quote_recovery_wait,
                   'submission_quote_buys':self.submission_quote_buys,
                   'submission_quote_sells':self.submission_quote_sells,
                   "source_label": "합성 검증 자료" if self.synthetic else "수신 시각을 보존한 시장 자료"}
        if self.strategy == 'pullback_simple':
            frame=self.selection_frame or {}
            if frame:
                self._lab_rows=[{'symbol':r['symbol'],'evaluated_at':frame.get('evaluated_at'),
                    'progress':r.get('indicators',{}).get('strategy_progress',{}),
                    'signal_present':bool(r.get('signal')),
                    'checks':r.get('indicators',{}).get('strategy_pattern',{}).get('checks',[]),
                    'llm_status':r.get('news_score_signal',{}).get('status','UNKNOWN'),
                    'llm_points':r.get('indicators',{}).get('score_components',{}).get('news',0)*10,
                    'analysis_ids':r.get('news_score_signal',{}).get('analysis_ids',[]),
                    'llm_call_ids':r.get('news_score_signal',{}).get('llm_call_ids',[]),
                    'order_status':r.get('portfolio_status'), 'order_intent_id':r.get('order_intent_id'),
                    'blockers':r.get('blockers',[])} for r in frame.get('rows',[])[:20]]
            context['strategy_lab']={'evaluated_at':self._lab_rows[0].get('evaluated_at') if self._lab_rows else None,
                'llm_role':'candidate_priority_only','historical_rvol_required':False,
                'rows':self._lab_rows}
        self.repo.set_control("sessionquant_context", context)
        if self.market.strict_data:
            from .market.reporting import persist_quality
            persist_quality(self,now)
        self.repo.set_control("runtime_kind", "sessionquant")
        self._last_summary = {k:v for k,v in broker_state.items() if k != 'last_event_at'}
        self._last_persist_at = now

    def _quote_entry_allowed(self, symbol, now):
        if self.quote_entry_guard is None:return True
        if symbol not in self._batch_quote_entry_allowed:
            self._batch_quote_entry_allowed[symbol]=bool(self.quote_entry_guard(symbol,now))
        return self._batch_quote_entry_allowed[symbol]

    def _quote_wait_evidence(self, symbol):
        if self.quote_entry_evidence is None:return {}
        try:
            value=self.quote_entry_evidence(symbol)
            keys=('reason','checked_at','connection_state','acknowledged','generation','entry_generation',
                  'quote_at','quote_received_at','quote_age_seconds','max_age_seconds',
                  'engine_quote_at','engine_quote_received_at','conflict_at')
            return {key:value[key] for key in keys if key in value
                    and (value[key] is None or type(value[key]) in (bool,int,float,str))
                    and len(str(value[key]))<=96} if isinstance(value,dict) else {}
        except Exception:
            # Diagnostics must not interrupt order/risk processing.
            return {'reason':'evidence_unavailable'}

    def _execution_eligibility_allowed(self, quote, now):
        from .market.scanner import eligibility_blockers
        return not eligibility_blockers(quote.symbol, self.market.eligibility.get(quote.symbol), now,
            max_eligibility_age_seconds=self.market.max_eligibility_age_seconds,
            require_evidence=self.market.strict_data,
            paper_assumptions=self.market.paper_data_policy == 'observed_paper_v1')

    def _buy_fill_blocker(self, order, quote, now):
        # Waiting preserves the existing order, never permission to fill from a
        # disconnected, conflicting, stale or engine-unacknowledged quote.
        if not self._quote_entry_allowed(quote.symbol, now):
            return 'websocket_quote_unavailable'
        return self.execution_guard.fill_blocker(order, quote, now)

    def _sell_fill_blocker(self, order, quote, now):
        if self.market.quote_is_conflicted(quote):return 'runtime_quote_stale_or_invalid'
        return self.sell_execution_guard.fill_blocker(order,quote,now)

    def _submit_exit(self, intent, quote, now):
        if not self.submission_quote_sells:return self.broker.submit_order(intent)
        return self.broker.submit_sell_with_quote(intent,quote,now,
            sell_fill_guard=lambda order,book:self._sell_fill_blocker(order,book,now))

    @timed('engine_batch')
    def apply_batch(self, events: list[dict], now: datetime, *, cursor: int | None = None, controls: dict | None = None):
        if self.failed:
            raise RuntimeError("저장 또는 처리 실패 후에는 재시작과 장부 확인이 필요합니다.")
        now = utc(now)
        if self.last_decision_at is not None and now<self.last_decision_at:
            raise ValueError('전략 처리 시각이 이전 처리 시각보다 과거입니다. 장부는 변경하지 않습니다.')
        previous = self.last_time
        for raw in events:
            available = event_time(raw)
            if available > now or (previous and available < previous):
                raise ValueError("이벤트가 미래 자료이거나 수신 시각 순서가 뒤바뀌었습니다.")
            previous = available
        self._batch_decisions = []
        self._batch_news_contexts = {}
        self._batch_candidate_scope=[]
        self.selection_frame=None
        self._batch_quote_entry_allowed = {}
        self._batch_quote_wait_evidence = {}
        self.execution_guard.begin_batch()
        self.sell_execution_guard.begin_batch()
        self._batch_scope_blockers = {} if self.watchlist_policy is not None or self.quote_entry_guard is not None else None
        try:
            scope = getattr(self.watchlist_policy, 'read_scope', nullcontext)
            with self.repo.transaction(), scope(), self.market.evaluation_scope():
                self.last_decision_at=now
                for raw in events:
                    self._ingest(raw)
                self.last_time = previous
                if cursor is not None:
                    self.cursor = cursor
                self.broker.roll_session(now)
                self.broker.expire_orders(now)
                if self.control_state is not None:
                    from .trading_controls import pending,acknowledge
                    stopped_this_cycle=False
                    for request in pending(self.repo,self.control_state,now):
                        action=request['action']
                        if action=='stop_all':
                            self.control_state.update(running=False,draining=True)
                            stopped_this_cycle=True
                        elif action=='start':
                            if self.control_state.get('completed'):
                                acknowledge(self.repo,request,ok=False,message='완료된 실행은 재개할 수 없습니다.'); continue
                            if self.control_state.get('data_ready') is False:
                                acknowledge(self.repo,request,ok=False,message='분봉 자료를 준비 중입니다. 완료 후 시작하세요.'); continue
                            if stopped_this_cycle or (self.control_state['draining'] and (self.broker.positions or self.broker.get_open_orders())):
                                acknowledge(self.repo,request,ok=False,message='종료·청산 중에는 시작할 수 없습니다.'); continue
                            self.control_state.update(running=True,draining=False)
                        elif action=='sell_symbol':
                            symbol=request['symbol']; position=self.broker.positions.get(symbol)
                            if not position:
                                acknowledge(self.repo,request,ok=False,message='현재 관리하는 보유 종목이 아닙니다.'); continue
                            self.broker.exit_intents.setdefault(symbol,{'reason':'manual_market_sell',
                                'opened_at':position.opened_at.isoformat(),'requested_at':now.isoformat(),'attempt':0})
                        acknowledge(self.repo,request)
                    if self.control_state['draining']:
                        for symbol,position in self.broker.positions.items():
                            self.broker.exit_intents.setdefault(symbol,{'reason':'operator_stop_all',
                                'opened_at':position.opened_at.isoformat(),'requested_at':now.isoformat(),'attempt':0})
                manual_halt = (self.repo.get_control("manual_halt", False) is True or
                    (self.control_state is not None and (not self.control_state['running']
                                                        or self.control_state.get('data_ready') is False)))
                if self.macro_reference_risk is not None:
                    self.reference_risk_status = self.macro_reference_risk.evaluate(now)
                reference_blocked = self.reference_risk_status.get('block_entries') is True
                for order in self.broker.get_open_orders():
                    if order.intent.side == 'buy':
                        risk = self.risk_context(now,order.intent.symbol)
                        if risk.get('block_entries') or risk.get('blackout'):
                            self.broker.cancel_order(order.client_id,now,reason='news_risk_restriction')
                        elif reference_blocked:
                            self.broker.cancel_order(order.client_id,now,reason='macro_reference_risk_restriction')
                        elif not self._quote_entry_allowed(order.intent.symbol,now):
                            if self.quote_recovery_wait:
                                if order.status in {'submitted','accepted','partial'}:
                                    order.unfilled_reason='websocket_quote_unavailable'
                                    if not order.quote_wait or order.quote_wait.get('recovered_at'):
                                        # Keep one small latest wait interval per order, not per-tick history.
                                        order.quote_wait={'started_at':now.isoformat(),'recovered_at':None}
                                        evidence=self._quote_wait_evidence(order.intent.symbol)
                                        if evidence:
                                            order.quote_wait['evidence']=evidence
                                            self._batch_quote_wait_evidence[order.intent.symbol]=evidence
                            else:
                                self.broker.cancel_order(order.client_id,now,reason='websocket_quote_unavailable')
                        elif order.quote_wait and not order.quote_wait.get('recovered_at'):
                            order.quote_wait['recovered_at']=now.isoformat()
                            if order.unfilled_reason=='websocket_quote_unavailable':
                                order.unfilled_reason='awaiting_post_acceptance_quote'
                if manual_halt:
                    for order in self.broker.get_open_orders():
                        if order.intent.side == "buy":
                            self.broker.cancel_order(order.client_id,now,reason='operator_stop_or_data_not_ready')
                session_status = self.exit_manager.tick(now, self.market.quotes)
                quotes = [parse_event(raw).data for raw in events if raw["kind"] == "quote"]
                for quote in quotes:
                    # All inputs are ingested first. A later conflicting book in
                    # this same batch invalidates both versions for execution.
                    if self.market.quote_is_conflicted(quote):
                        continue
                    paper_tradable = None
                    if self.market.paper_data_policy == 'observed_paper_v1':
                        from .market.paper_policy import paper_eligibility
                        paper_tradable = (paper_eligibility(self.market.eligibility.get(quote.symbol),now)['allowed']
                            and (self.quote_recovery_wait or self._quote_entry_allowed(quote.symbol,now)))
                    self.broker.process_quote(quote, now, paper_tradable=paper_tradable,
                        buy_fill_guard=(lambda order, book: self._buy_fill_blocker(order, book, now))
                            if self.execution_guard.enabled or self.quote_entry_guard is not None else None,
                        sell_fill_guard=(lambda order,book:self._sell_fill_blocker(order,book,now))
                            if self.submission_quote_sells else None)
                for symbol in list(self.broker.positions):
                    quote = self.market.quotes.get(symbol)
                    risk = self.risk_context(now,symbol)
                    self.manager.check_exits(quote,now,symbol=symbol,bearish_signal=self._bearish(symbol,now),
                                             confirmed_adverse=risk.get('major_adverse') is True)
                decisions = []
                from .market_reference.contracts import REFERENCE_SYMBOLS
                symbols={q.symbol for q in quotes}-REFERENCE_SYMBOLS
                # Candles and cached listing evidence can arrive after quotes.
                # Re-evaluate with the original quote timestamps; freshness,
                # entry-window and portfolio gates still apply without changes.
                symbols.update(raw['data']['symbol'] for raw in events
                    if raw['kind'] in {'bar','eligibility'}
                    and raw['data']['symbol'] in self.market.quotes
                    and raw['data']['symbol'] not in REFERENCE_SYMBOLS
                    and self.broker.quote_is_fresh(self.market.quotes[raw['data']['symbol']], now))
                selection_enabled=self.candidate_selector.settings.enabled
                # 위험 시계만의 tick은 계속 가볍게 처리한다. 후보 입력이 오면 전체 집중 범위를 같은 now로 평가한다.
                if selection_enabled and symbols:
                    scope=self.watchlist_policy
                    if scope is not None and hasattr(scope,'candidate_symbols'):symbols=set(scope.candidate_symbols(now))|symbols
                    elif scope is None:symbols=set(self.universe.members(now))|symbols
                self._batch_candidate_scope=sorted(symbols-REFERENCE_SYMBOLS)
                scope_expiries={}
                for symbol in self._batch_candidate_scope:
                    risk = self.risk_context(now, symbol)
                    decision = self.market.evaluate(symbol, now, news_blocked=bool(risk.get("block_entries") or risk.get("blackout")))
                    if reference_blocked:
                        if decision.signal is not None:
                            self.repo.record('pipeline_status',{'id':'reference-block:'+decision.signal.id, 'kind':'reference_entry_block',
                                'signal_id':decision.signal.id,'symbol':symbol,'timestamp':now,'status':self.reference_risk_status.get('status')})
                        decision = replace(decision, eligible=False, signal=None,
                            blockers=tuple(dict.fromkeys([*decision.blockers, 'macro_reference_risk_restriction'])))
                    if self.watchlist_policy is not None:
                        scope_blockers = self.watchlist_policy.entry_blockers(symbol, now)
                        if selection_enabled and hasattr(self.watchlist_policy,'candidate_expiry'):
                            try:
                                expiry=self.watchlist_policy.candidate_expiry(symbol,now)
                                if expiry:
                                    scope_expiries[symbol]=utc(expiry).isoformat()
                                    if utc(expiry)<=now:scope_blockers=[*scope_blockers,'focus_time_expired']
                            except (ValueError,TypeError,KeyError,AttributeError):scope_blockers=[*scope_blockers,'focus_time_unverified']
                        self._batch_scope_blockers[symbol]=list(scope_blockers)
                        if scope_blockers:
                            decision = replace(decision, eligible=False, signal=None,
                                blockers=tuple(dict.fromkeys([*decision.blockers, *scope_blockers])))
                    if self._batch_scope_blockers is not None:
                        self._batch_scope_blockers.setdefault(symbol,[])
                    if not self._quote_entry_allowed(symbol,now):
                        code='websocket_quote_unavailable'
                        self._batch_scope_blockers.setdefault(symbol,[]).append(code)
                        decision=replace(decision,eligible=False,signal=None,
                            blockers=tuple(dict.fromkeys([*decision.blockers,code])))
                    decisions.append((decision, risk))
                    if self.market.strict_data:
                        self.market.quality_rows[symbol]['blockers'] = list(decision.blockers)
                ranked=None;frame=None;strength=None
                if selection_enabled and decisions:
                    ranked,frame,strength=self.candidate_selector.rank(decisions,self.market,self.broker,now,
                        synthetic=self.synthetic,manual_halt=manual_halt,consumed=self.consumed_signals,scope_expiries=scope_expiries)
                    decisions=[(decision,risk) for decision,risk,_ in ranked]
                records={row['symbol']:row for _,_,row in (ranked or [])}
                for decision,risk in decisions:
                    self._batch_decisions.append(decision.to_dict())
                    # Ranked candidates are saved below after allocation has supplied
                    # their final state, in this same transaction.
                    if frame is None:
                        self.repo.record('candidates',{'id':decision.symbol,'timestamp':now,**decision.to_dict(),'news_risk':risk})
                for decision, risk in sorted(decisions, key=lambda item: (-item[0].score, item[0].symbol)):
                    signal = decision.signal
                    selected=records.get(decision.symbol)
                    if selected is not None:
                        blockers=[*selected['blockers'],*self.candidate_selector.admission_blockers(selected,self.broker,strength)]
                        if blockers:
                            selected.update(portfolio_status='BLOCKED',blockers=list(dict.fromkeys(blockers)))
                            continue
                    if signal is None or signal.id in self.consumed_signals or manual_halt:
                        continue
                    self.repo.record("signals", signal.to_dict())
                    quote = self.market.quotes[decision.symbol]
                    evidence = self.market.eligibility[decision.symbol]
                    add = decision.symbol in self.broker.positions
                    if add and not signal.allow_add:
                        if selected is not None:selected.update(portfolio_status='BLOCKED',allocation_reason='add_conditions_not_met')
                        continue
                    stop = self.exit_policy.stop_price(quote.ask,signal.stop_price,decision.indicators.get('atr14'))
                    if stop is None:
                        if selected is not None:selected.update(portfolio_status='BLOCKED',allocation_reason='exit_stop_unverified')
                        self.repo.record('pipeline_status',{'id':'risk:'+signal.id,'kind':'risk_decision','symbol':decision.symbol,
                            'timestamp':now,'approved':False,'reason':'exit_stop_unverified','exit_policy':serialize(self.exit_policy)})
                        continue
                    if selected is not None:
                        if frame['final_candidate_count']>=self.candidate_selector.settings.final_candidate_limit:
                            selected.update(portfolio_status='BLOCKED',blockers=[*selected['blockers'],'final_candidate_limit'])
                            continue
                        selected.update(final_candidate=True,portfolio_status='FINAL_CANDIDATE')
                        frame['final_candidate_count']+=1
                    allocation = self.manager.plan_entry(decision.symbol, quote.ask, stop, now,bid=quote.bid,
                        market_state=decision.regime if self.use_market_filter else "BULLISH",
                        tradable=(decision.indicators.get('paper_eligibility') or {}).get('allowed',evidence.tradable is True),
                        fractional_supported=(decision.indicators.get('paper_eligibility') or {}).get('allowed',evidence.fractional is True), trend_confirmed=True,
                        support_confirmed=signal.allow_add, rebound_confirmed=signal.allow_add,
                        major_bad_news=risk.get("major_adverse") is True, add=add, signal_id=signal.id,
                        session_allowed=self.exit_manager.permits_entry(now), strategy=self.strategy)
                    risk_decision={'id':'risk:'+signal.id,'kind':'risk_decision','symbol':decision.symbol,
                        'timestamp':now,'approved':allocation.approved,'reason':allocation.reason,'audit':allocation.audit,'exit_policy':serialize(self.exit_policy),
                        **({'selection_id':frame['id'],'candidate_rank':selected['rank'],'candidate_score':decision.score} if frame else {})}
                    self.repo.record('pipeline_status',risk_decision)
                    if selected is not None:
                        selected.update(portfolio_status='APPROVED' if allocation.approved else 'ALLOCATION_REJECTED',
                            allocation_reason=allocation.reason,approved=allocation.approved,allocation_audit=allocation.audit,
                            order_intent_id=allocation.intent.client_id if allocation.approved else None)
                    if allocation.approved:
                        intent_record=serialize(allocation.intent)|{'signal_id':signal.id,'risk_audit':allocation.audit,'risk_policy_version':2,
                            'paper_data_policy':self.market.paper_data_policy,
                            'paper_assumptions':decision.indicators.get('paper_eligibility'),
                            'strategy_rvol':decision.indicators.get('relative_volume'),
                            'strategy_rvol_status':decision.indicators.get('data_quality',{}).get('strategy_rvol_status'),
                            **({'selection_id':frame['id'],'candidate_rank':selected['rank'],'candidate_score':decision.score,
                                'selection_policy_version':frame['policy_version']} if frame else {})}
                        execution=self.execution_guard.check('submission',allocation.intent,quote,now)
                        if execution is not None and not execution['allowed']:
                            self.repo.record('pipeline_status',{**risk_decision,'approved':False,
                                'reason':execution['reason'],'execution_check':execution})
                            if selected is not None:
                                selected.update(portfolio_status='ALLOCATION_REJECTED',approved=False,
                                    allocation_reason=execution['reason'],order_intent_id=None,execution_check=execution)
                            continue
                        if self.submission_quote_buys:
                            tradable=(decision.indicators.get('paper_eligibility') or {}).get('allowed',evidence.tradable is True)
                            self.broker.submit_buy_with_quote(allocation.intent,quote,now,paper_tradable=tradable,
                                buy_fill_guard=lambda order,book: self._buy_fill_blocker(order,book,now))
                        else:
                            self.broker.submit_order(allocation.intent)
                        # Keep audit persistence in this transaction, after the
                        # final execution check and in-memory submission.
                        self.repo.record("order_intents",intent_record)
                        self.consumed_signals.add(signal.id)
                        if frame:
                            frame['approved_count']+=1
                            frame['approved_add_count' if add else 'approved_new_count']+=1
                if frame:
                    from .portfolio.selection import VIEW
                    if self.selection_source_id:frame['source_selection_id']=self.selection_source_id
                    frame['macro_reference_risk']=self.reference_risk_status
                    frame['exposed_symbols']=sorted(set(self.broker.positions)|{order.intent.symbol for order in self.broker.get_open_orders() if order.intent.side=='buy'})
                    for decision,risk,row in ranked:
                        row['exposure_overlap']=self.candidate_selector.exposure_overlap(row,self.broker,strength)
                        self.repo.record('candidates',{'id':decision.symbol,'timestamp':now,**decision.to_dict(),
                            'news_risk':risk,'selection_id':frame['id'],'selection':row})
                    self.repo.record('pipeline_status',frame)
                    self.repo.set_control(VIEW,frame)
                    self.selection_frame=frame
                session_status = self.exit_manager.snapshot(now,orders=session_status['orders'])
                if self.control_state is not None:
                    from .trading_controls import heartbeat
                    heartbeat(self.repo,self.control_state,now=now,flat=not self.broker.positions and not self.broker.get_open_orders())
                changed = self._last_summary != {k:v for k,v in self.broker.to_state().items() if k != 'last_event_at'}
                heartbeat_due = self._last_persist_at is None or (now - self._last_persist_at).total_seconds() >= 30
                if events or changed or heartbeat_due or controls:
                    self._persist(now, session_status)
                if self.record_inputs:
                    from .research.journal import frame
                    frame(self,events,now,manual_halt=manual_halt)
                for name, value in (controls or {}).items():
                    self.repo.set_control(name, value)
        except BaseException:
            # 주문 결과를 추측하거나 손상된 메모리 상태로 처리를 재개하지 않는다.
            self.failed = True
            raise

    def tick(self, now: datetime):
        """시세·뉴스 요청이 늦어져도 독립적으로 청산 시각과 주문 만료를 점검한다."""
        self.apply_batch([], now)
