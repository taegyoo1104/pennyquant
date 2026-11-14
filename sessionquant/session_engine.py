"""시장·뉴스 위험·소수점 모의 주문을 연결하는 단일 작성자 엔진.

뉴스 API는 이 모듈에서 호출하지 않는다. 저장 실패 후에는 같은 인스턴스로
주문을 계속하지 않으며, 재시작 시 커밋된 장부와 수신 커서부터 복구한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from hashlib import sha256
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


def event_time(raw: dict) -> datetime:
    """외부 이벤트가 실제 사용 가능해진 시각을 읽는다."""
    data = raw["data"]
    field = {"eligibility": "checked_at", "rvol": "observed_at", "risk_context": "available_at"}.get(raw["kind"], "received_at")
    return utc(data[field])


class SessionEngine:
    def __init__(self, repository: Repository, universe: UniverseStore, *, strategy: str = "pullback",
                 settings: PortfolioSettings | None = None, restore: bool = False, risk_provider=None,
                 synthetic: bool = False, use_news: bool = True, use_market_filter: bool = True, control_state: dict | None = None, watchlist_policy=None,
                 strict_market_data: bool = False, exit_policy: ExitPolicy | None = None, macro_reference_risk=None):
        self.macro_reference_risk = macro_reference_risk
        self.reference_risk_status = {}
        if macro_reference_risk is not None and macro_reference_risk.effective_enabled and not synthetic:
            raise ValueError('위험 필터 ON 정책은 격리 합성 연구에서만 검증할 수 있습니다.')
        self.control_state=control_state
        self.watchlist_policy=watchlist_policy
        self.repo, self.universe = repository, universe
        self.strategy, self.synthetic, self.use_news = strategy, synthetic, use_news
        self.use_market_filter = use_market_filter
        self.market = MarketEngine(universe, strategy, use_market_filter=use_market_filter, strict_data=strict_market_data)
        self.broker = PaperBroker(settings or PortfolioSettings())
        self.failed = False
        self.risk_provider = risk_provider
        self.last_time = None
        self.cursor = 0
        self.risk_records: dict[str, dict] = {}
        self.risk_provider_cache = {}
        self._risk_lookup_errors = set()
        self.exit_policy = exit_policy or ExitPolicy()
        self.consumed_signals: set[str] = set()
        self.dirty_symbols: set[str] = set()
        self.market_symbols: set[str] = set()
        self._journal_hashes: dict[str, str] = {}
        self._last_summary = None
        self._last_persist_at = None
        stored = self.repo.load_state()
        if stored is not None:
            if not restore:
                raise ValueError("이미 저장된 상태가 있습니다. 복구 옵션 또는 별도 DB를 사용하세요.")
            if (stored.get("schema_version") != 2 or stored.get("environment") != "PAPER_LOCAL"
                    or (stored.get('strict_market_data',False) and not strict_market_data)
                    or stored.get("strategy") != strategy or stored.get("synthetic") != synthetic
                    or stored.get("use_news", True) != use_news
                    or stored.get("use_market_filter", True) != use_market_filter):
                raise ValueError("저장된 실행 환경·전략·자료 유형이 다릅니다. 기존 기록을 덮어쓰지 않습니다.")
            self.broker = PaperBroker.from_state(stored["broker"], settings)
            self.last_time = utc(stored["last_time"]) if stored.get("last_time") else None
            self.cursor = stored.get("cursor", 0)
            self.consumed_signals = set(stored.get("consumed_signals", []))
            self.risk_records = stored.get("risk_records", {})
            if ExitPolicy(**stored.get('exit_policy',{})) != self.exit_policy:
                raise ValueError('손절·청산 연구 정책이 저장된 실행과 다릅니다. 새 DB를 사용하세요.')
            self.risk_provider_cache = stored.get('risk_provider_cache',{})
            self.market_symbols = set(stored.get("market_symbols", []))
            merged = {"schema_version": 1, **{name: {} for name in ("bars", "quotes", "eligibility", "rvol", "sector_strength")}}
            for symbol in sorted(self.market_symbols):
                chunk = self.repo.get_control("sq_market_" + symbol)
                if not chunk:
                    raise ValueError("시장 복구 자료가 누락되어 신규 주문을 중단합니다.")
                for name in merged:
                    if name != "schema_version":
                        merged[name].update(chunk.get(name, {}))
            self.market.restore(merged)
        elif self.repo.get_control("runtime_kind") not in (None, "sessionquant"):
            raise ValueError("기존 PennyQuant DB는 호환 실행 경로로 열거나 새 DB를 지정하세요.")
        self.manager = PortfolioManager(self.broker,self.exit_policy)
        self.exit_manager = SessionExitManager(self.broker)

    def risk_context(self, now: datetime, symbol: str) -> dict:
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

    def _persist(self, now: datetime, session_status: dict):
        risk_snapshot = self.manager.risk_snapshot(now)
        session_status['alerts'] = list(self.broker.alerts)
        # 종목별 시장 상태를 분리하여 매 호가마다 전체 유니버스를 다시 저장하지 않는다.
        metadata = self.market.to_state(include_bars=False)
        for symbol in sorted(self.dirty_symbols):
            payload = {"schema_version": 1, "bars": {symbol: jsonable(self.market.bars.get(symbol, []))}}
            for name in ("quotes", "eligibility", "rvol", "sector_strength"):
                payload[name] = {symbol: metadata[name][symbol]} if symbol in metadata[name] else {}
            self.repo.set_control("sq_market_" + symbol, payload)
            self.market_symbols.add(symbol)
        self.dirty_symbols.clear()
        broker_state = self.broker.to_state()
        state = {"schema_version": 2, "environment": "PAPER_LOCAL", "strategy": self.strategy,
                 'risk_policy_version':2,'exit_policy':serialize(self.exit_policy),'risk_provider_cache':self.risk_provider_cache,
                 "strict_market_data":self.market.strict_data,
                 "synthetic": self.synthetic, "use_news": self.use_news, "use_market_filter": self.use_market_filter, "broker": broker_state,
                 "cursor": self.cursor, "last_time": self.last_time.isoformat() if self.last_time else None,
                 "market_symbols": sorted(self.market_symbols), "consumed_signals": sorted(self.consumed_signals),
                 "risk_records": self.risk_records}
        self.repo.save_state(state)
        for order in self.broker.orders.values():
            row = serialize(order)
            row.update(serialize(order.intent))
            row['expected_price'] = serialize(order.expected_price)
            self._record_changed("orders", order.client_id, row)
        for fill in self.broker.fills:
            self._record_changed("executions", fill.id, serialize(fill))
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
                   "market_regime": regime, "session": serialize(session_status), "account": account,
                   "backend": self.repo.backend, "news_status": "UNKNOWN" if self.risk_provider is None else 'DEGRADED' if self._risk_lookup_errors else "ENABLED",
                   'risk_policy_version':2,'exit_policy':serialize(self.exit_policy),'risk_snapshot':risk_snapshot,
                   'macro_reference_risk':self.reference_risk_status,
                   "paper_fill_model":self.broker.settings.paper_fill_model, "paper_ledger_version":2,
                   "source_label": "합성 검증 자료" if self.synthetic else "수신 시각을 보존한 시장 자료"}
        self.repo.set_control("sessionquant_context", context)
        if self.market.strict_data:
            from .market.reporting import persist_quality
            persist_quality(self,now)
        self.repo.set_control("runtime_kind", "sessionquant")
        self._last_summary = {k:v for k,v in broker_state.items() if k != 'last_event_at'}
        self._last_persist_at = now

    def apply_batch(self, events: list[dict], now: datetime, *, cursor: int | None = None, controls: dict | None = None):
        if self.failed:
            raise RuntimeError("저장 또는 처리 실패 후에는 재시작과 장부 확인이 필요합니다.")
        now = utc(now)
        previous = self.last_time
        for raw in events:
            available = event_time(raw)
            if available > now or (previous and available < previous):
                raise ValueError("이벤트가 미래 자료이거나 수신 시각 순서가 뒤바뀌었습니다.")
            previous = available
        try:
            with self.repo.transaction():
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
                    (self.control_state is not None and not self.control_state['running']))
                if self.macro_reference_risk is not None:
                    self.reference_risk_status = self.macro_reference_risk.evaluate(now)
                reference_blocked = self.reference_risk_status.get('block_entries') is True
                for order in self.broker.get_open_orders():
                    if order.intent.side == 'buy':
                        risk = self.risk_context(now,order.intent.symbol)
                        if risk.get('block_entries') or risk.get('blackout') or reference_blocked:
                            self.broker.cancel_order(order.client_id,now)
                if manual_halt:
                    for order in self.broker.get_open_orders():
                        if order.intent.side == "buy":
                            self.broker.cancel_order(order.client_id,now)
                session_status = self.exit_manager.tick(now, self.market.quotes)
                quotes = [parse_event(raw).data for raw in events if raw["kind"] == "quote"]
                for quote in quotes:
                    self.broker.process_quote(quote, now)
                for symbol in list(self.broker.positions):
                    quote = self.market.quotes.get(symbol)
                    risk = self.risk_context(now,symbol)
                    self.manager.check_exits(quote,now,symbol=symbol,bearish_signal=self._bearish(symbol,now),
                                             confirmed_adverse=risk.get('major_adverse') is True)
                decisions = []
                for symbol in {q.symbol for q in quotes} - {"SPY", "QQQ"}:
                    risk = self.risk_context(now, symbol)
                    decision = self.market.evaluate(symbol, now, news_blocked=bool(risk.get("block_entries") or risk.get("blackout")))
                    if reference_blocked:
                        decision = replace(decision, eligible=False, signal=None,
                            blockers=tuple(dict.fromkeys([*decision.blockers, 'macro_reference_risk_restriction'])))
                    if self.watchlist_policy is not None:
                        scope_blockers = self.watchlist_policy.entry_blockers(symbol, now)
                        if scope_blockers:
                            decision = replace(decision, eligible=False, signal=None,
                                blockers=tuple(dict.fromkeys([*decision.blockers, *scope_blockers])))
                    decisions.append((decision, risk))
                    if self.market.strict_data:
                        self.market.quality_rows[symbol]['blockers'] = list(decision.blockers)
                    self.repo.record("candidates", {"id": symbol, "timestamp": now, **decision.to_dict(), "news_risk": risk})
                for decision, risk in sorted(decisions, key=lambda item: (-item[0].score, item[0].symbol)):
                    signal = decision.signal
                    if signal is None or signal.id in self.consumed_signals or manual_halt:
                        continue
                    self.repo.record("signals", signal.to_dict())
                    quote = self.market.quotes[decision.symbol]
                    evidence = self.market.eligibility[decision.symbol]
                    add = decision.symbol in self.broker.positions
                    if add and not signal.allow_add:
                        continue
                    stop = self.exit_policy.stop_price(quote.ask,signal.stop_price,decision.indicators.get('atr14'))
                    if stop is None:
                        self.repo.record('pipeline_status',{'id':'risk:'+signal.id,'kind':'risk_decision','symbol':decision.symbol,
                            'timestamp':now,'approved':False,'reason':'exit_stop_unverified','exit_policy':serialize(self.exit_policy)})
                        continue
                    allocation = self.manager.plan_entry(decision.symbol, quote.ask, stop, now,bid=quote.bid,
                        market_state=decision.regime if self.use_market_filter else "BULLISH", tradable=evidence.tradable is True,
                        fractional_supported=evidence.fractional is True, trend_confirmed=True,
                        support_confirmed=signal.allow_add, rebound_confirmed=signal.allow_add,
                        major_bad_news=risk.get("major_adverse") is True, add=add, signal_id=signal.id,
                        session_allowed=self.exit_manager.permits_entry(now), strategy=self.strategy)
                    self.repo.record('pipeline_status',{'id':'risk:'+signal.id,'kind':'risk_decision','symbol':decision.symbol,
                        'timestamp':now,'approved':allocation.approved,'reason':allocation.reason,'audit':allocation.audit,'exit_policy':serialize(self.exit_policy)})
                    if allocation.approved:
                        self.repo.record("order_intents", serialize(allocation.intent)|{'risk_audit':allocation.audit,'risk_policy_version':2})
                        self.broker.submit_order(allocation.intent)
                        self.consumed_signals.add(signal.id)
                session_status = self.exit_manager.snapshot(now,orders=session_status['orders'])
                if self.control_state is not None:
                    from .trading_controls import heartbeat
                    heartbeat(self.repo,self.control_state,now=now,flat=not self.broker.positions and not self.broker.get_open_orders())
                changed = self._last_summary != {k:v for k,v in self.broker.to_state().items() if k != 'last_event_at'}
                heartbeat_due = self._last_persist_at is None or (now - self._last_persist_at).total_seconds() >= 30
                if events or changed or heartbeat_due or controls:
                    self._persist(now, session_status)
                for name, value in (controls or {}).items():
                    self.repo.set_control(name, value)
        except BaseException:
            # 주문 결과를 추측하거나 손상된 메모리 상태로 처리를 재개하지 않는다.
            self.failed = True
            raise

    def tick(self, now: datetime):
        """시세·뉴스 요청이 늦어져도 독립적으로 청산 시각과 주문 만료를 점검한다."""
        self.apply_batch([], now)
