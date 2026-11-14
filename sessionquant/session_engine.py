"""시장·뉴스 위험·소수점 모의 주문을 연결하는 단일 작성자 엔진.

뉴스 API는 이 모듈에서 호출하지 않는다. 저장 실패 후에는 같은 인스턴스로
주문을 계속하지 않으며, 재시작 시 커밋된 장부와 수신 커서부터 복구한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def event_time(raw: dict) -> datetime:
    """외부 이벤트가 실제 사용 가능해진 시각을 읽는다."""
    data = raw["data"]
    field = {"eligibility": "checked_at", "rvol": "observed_at", "risk_context": "available_at"}.get(raw["kind"], "received_at")
    return utc(data[field])


class SessionEngine:
    def __init__(self, repository: Repository, universe: UniverseStore, *, strategy: str = "pullback",
                 settings: PortfolioSettings | None = None, restore: bool = False, risk_provider=None,
                 synthetic: bool = False, use_news: bool = True, use_market_filter: bool = True):
        self.repo, self.universe = repository, universe
        self.strategy, self.synthetic, self.use_news = strategy, synthetic, use_news
        self.use_market_filter = use_market_filter
        self.market = MarketEngine(universe, strategy, use_market_filter=use_market_filter)
        self.broker = PaperBroker(settings or PortfolioSettings())
        self.failed = False
        self.risk_provider = risk_provider
        self.last_time = None
        self.cursor = 0
        self.risk_records: dict[str, dict] = {}
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
                    or stored.get("strategy") != strategy or stored.get("synthetic") != synthetic
                    or stored.get("use_news", True) != use_news
                    or stored.get("use_market_filter", True) != use_market_filter):
                raise ValueError("저장된 실행 환경·전략·자료 유형이 다릅니다. 기존 기록을 덮어쓰지 않습니다.")
            self.broker = PaperBroker.from_state(stored["broker"], settings)
            self.last_time = utc(stored["last_time"]) if stored.get("last_time") else None
            self.cursor = stored.get("cursor", 0)
            self.consumed_signals = set(stored.get("consumed_signals", []))
            self.risk_records = stored.get("risk_records", {})
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
        self.manager = PortfolioManager(self.broker)
        self.exit_manager = SessionExitManager(self.broker)

    def risk_context(self, now: datetime, symbol: str) -> dict:
        empty = {"status": "UNKNOWN", "block_entries": False, "major_adverse": False,
                 "blackout": False, "reasons": ["뉴스 상태 미확인"]}
        if not self.use_news:
            return empty | {"status": "DISABLED"}
        if self.risk_provider:
            return self.risk_provider.risk_context(now, symbol=symbol)
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
                 "synthetic": self.synthetic, "use_news": self.use_news, "use_market_filter": self.use_market_filter, "broker": broker_state,
                 "cursor": self.cursor, "last_time": self.last_time.isoformat() if self.last_time else None,
                 "market_symbols": sorted(self.market_symbols), "consumed_signals": sorted(self.consumed_signals),
                 "risk_records": self.risk_records}
        self.repo.save_state(state)
        for order in self.broker.orders.values():
            row = serialize(order)
            row.update(serialize(order.intent))
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
                   "backend": self.repo.backend, "news_status": "UNKNOWN" if self.risk_provider is None else "ENABLED",
                   "source_label": "합성 검증 자료" if self.synthetic else "수신 시각을 보존한 시장 자료"}
        self.repo.set_control("sessionquant_context", context)
        self.repo.set_control("runtime_kind", "sessionquant")
        self._last_summary = broker_state
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
                manual_halt = self.repo.get_control("manual_halt", False) is True
                if manual_halt:
                    for order in self.broker.get_open_orders():
                        if order.intent.side == "buy":
                            self.broker.cancel_order(order.client_id)
                session_status = self.exit_manager.tick(now, self.market.quotes)
                quotes = [parse_event(raw).data for raw in events if raw["kind"] == "quote"]
                for quote in quotes:
                    self.broker.process_quote(quote, now)
                for symbol in list(self.broker.positions):
                    quote = self.market.quotes.get(symbol)
                    if quote:
                        risk = self.risk_context(now, symbol)
                        self.manager.check_exits(quote, now, bearish_signal=self._bearish(symbol, now),
                                                 confirmed_adverse=risk.get("major_adverse") is True)
                decisions = []
                for symbol in {q.symbol for q in quotes} - {"SPY", "QQQ"}:
                    risk = self.risk_context(now, symbol)
                    decision = self.market.evaluate(symbol, now, news_blocked=bool(risk.get("block_entries") or risk.get("blackout")))
                    decisions.append((decision, risk))
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
                    allocation = self.manager.plan_entry(decision.symbol, quote.ask, signal.stop_price, now,
                        market_state=decision.regime if self.use_market_filter else "BULLISH", tradable=evidence.tradable is True,
                        fractional_supported=evidence.fractional is True, trend_confirmed=True,
                        support_confirmed=signal.allow_add, rebound_confirmed=signal.allow_add,
                        major_bad_news=risk.get("major_adverse") is True, add=add, signal_id=signal.id,
                        session_allowed=self.exit_manager.permits_entry(now), strategy=self.strategy)
                    if allocation.approved:
                        self.repo.record("order_intents", serialize(allocation.intent))
                        self.broker.submit_order(allocation.intent)
                        self.consumed_signals.add(signal.id)
                changed = self._last_summary != self.broker.to_state()
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
