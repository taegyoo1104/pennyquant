"""Receipt-ordered strategy engine with injected paper or broker execution."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config.settings import Settings
from .database.repository import Repository
from .events import Event
from .models import Account, Bar, NewsEvent, Quote, StockSnapshot, jsonable, utc
from .news.classifier import classify
from .scanners.stock_scanner import scan
from .strategies.base import StrategyContext
from .strategies import STRATEGIES
from .trading.order_manager import PaperOrderManager
from .trading.order_manager import KNOWN_ACTIVE

NY = ZoneInfo("America/New_York")


class EventEngine:
    def __init__(self, settings: Settings, strategy: str = "pullback", repository: Repository | None = None,
                 *, restore: bool = False, live: bool = False, manager=None):
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy}")
        self.settings = settings
        self.strategy_name = strategy
        self.strategy = STRATEGIES[strategy]()
        self.repository = repository
        self.live = live
        self.manager = manager if manager is not None else PaperOrderManager(settings, Account(cash=settings.initial_cash,
            equity=settings.initial_cash, day_start_equity=settings.initial_cash))
        self.bars: dict[str, list[Bar]] = {}
        self.snapshots: dict[str, StockSnapshot] = {}
        self.quotes: dict[str, Quote] = {}
        self.news: dict[str, NewsEvent] = {}
        self.analyses: dict[str, dict] = {}
        self.equity_curve: list[dict] = []
        self.last_time: datetime | None = None
        self.signal_count = 0
        self.rejected_signals = 0
        self._session = None
        self._journal_orders: dict[str, dict] = {}
        self._journal_fills: set[str] = set()
        self._journal_alerts: set[str] = set()
        if restore and repository and (state := repository.load_state()):
            self.manager.restore_state(state)
            context = repository.get_control("engine_context", {})
            if context.get("strategy") != strategy:
                raise ValueError("persisted strategy differs; use a fresh DB")
            from .events import parse_event
            for raw in context.get("bars", []):
                bar = parse_event({"kind": "bar", "data": raw}).data
                self.bars.setdefault(bar.symbol, []).append(bar)
            for raw in context.get("snapshots", []):
                snap = parse_event({"kind": "snapshot", "data": raw}).data
                self.snapshots[snap.quote.symbol] = snap
                self.quotes[snap.quote.symbol] = snap.quote
            for raw in context.get("news", []):
                item = parse_event({"kind": "news", "data": raw}).data
                self.news[f"{item.id}:{item.received_at.isoformat()}"] = item
            # Quote ticks may be newer than the last scanner snapshot.
            from .events import parse_quote
            for symbol, raw in state.get("latest_quotes", {}).items():
                self.quotes[symbol] = parse_quote(raw)
            self.last_time = utc(context["last_time"]) if context.get("last_time") else None
            self._session = self.last_time.astimezone(NY).date() if self.last_time else None
            self.signal_count = context.get("signal_count", 0)
            self.rejected_signals = context.get("rejected_signals", 0)
            self._journal_orders = {item["client_id"]: item for item in repository.all("orders")}
            self._journal_fills = {item["id"] for item in repository.all("executions")}
            self.analyses = context.get("analyses", {})
            self._journal_alerts = {item["id"] for item in repository.all("alerts")}

    @property
    def live_orders_enabled(self) -> bool:
        broker = getattr(self.manager, "broker", None)
        return bool(getattr(self.manager, "execution_mode", "paper") == "live"
                    and broker is not None and broker.live_enabled and broker.acknowledge_live_orders
                    and broker.reconciled and not broker.halt_reasons and not self.manager.halt_reasons)

    def register_analysis(self, analysis) -> None:
        """Make completed analysis available without backdating provider receipts.

        News arrives before an asynchronous LLM result. Registering its original
        event is independent of the market event cursor, and the news gate checks
        analysis completion before allowing a subsequent strategy observation.
        """
        payload = analysis.to_dict()
        payload["id"] = payload.get("analysis_id") or f"{analysis.event.id}:{analysis.ticker}"
        event = analysis.event
        if not isinstance(event, NewsEvent):
            raise ValueError("analysis requires its original news event")
        ticker = analysis.ticker
        if ticker not in event.symbols:
            raise ValueError("analysis ticker is not associated with the original news event")
        self.analyses[f"{event.id}:{ticker}"] = payload
        self.news[f"{event.id}:{event.received_at.isoformat()}"] = event
        rule = classify(event)
        if (analysis.sentiment in {"NEGATIVE", "MIXED"} or analysis.dilution_risk
                or rule.sentiment == "negative" or rule.dilution_risk):
            available = analysis.analyzed_at or datetime.now(timezone.utc)
            for order in list(self.manager.orders.values()):
                if order.symbol == ticker and order.side == "buy" and order.status in KNOWN_ACTIVE:
                    self.manager.cancel(order.client_id, available)
            alert = {"id": f"news-risk:{payload['id']}", "type": "news_risk", "code": "news_risk",
                "level": "WARNING", "symbol": ticker, "timestamp": available,
                "message": f"{ticker}: 악재·혼합 사건 또는 희석 위험이 확인되어 신규 매수를 제한합니다."}
            alerts = getattr(self.manager, "alerts", None)
            if alerts is not None and not any(item["id"] == alert["id"] for item in alerts):
                alerts.append(jsonable(alert))
                del alerts[:-1000]
                callback = getattr(self.manager, "on_alert", None)
                if callback:
                    try:
                        callback(jsonable(alert))
                    except Exception:
                        if self.repository:
                            self.repository.log("ERROR", "News risk notification delivery failed")
        if self.repository:
            self.repository.record("news_analysis", payload)
            self.repository.record("news_events", {**jsonable(event), "classification": {
                "sentiment": analysis.sentiment.lower(), "event_type": "analyzed_event",
                "reason": analysis.reason, "exclude": not analysis.entry_eligible,
                "dilution_risk": analysis.dilution_risk}})
        self.persist()

    def apply_runtime_controls(self, now: datetime) -> None:
        """Apply an idle stop before broker dispatch, even without a new tick."""
        now = utc(now)
        if self.repository and self.repository.get_control("manual_halt", False):
            self.manager.halt("manual")
        else:
            self.manager.resume("manual")
        for symbol in self.manager.account.positions:
            quote = self.quotes.get(symbol)
            if (quote is None or utc(quote.timestamp) > utc(quote.received_at) or quote.received_at > now
                    or (now - utc(quote.timestamp)).total_seconds() > self.settings.max_quote_age_seconds
                    or (now - utc(quote.received_at)).total_seconds() > self.settings.max_quote_age_seconds):
                self.manager.halt("stale_market_data")
                break

    def process(self, event: Event) -> None:
        received = event.available_at
        if self.last_time is not None and received < self.last_time:
            raise ValueError("out-of-order receipt timestamp")
        now = datetime.now(timezone.utc) if self.live else received
        session = now.astimezone(NY).date()
        if self._session != session:
            self.bars.clear()
            self.snapshots.clear()
            self.quotes.clear()
            self._session = session
        self.last_time = received
        self.manager.roll_day(now)
        if self.repository:
            if self.repository.get_control("manual_halt", False):
                self.manager.halt("manual")
            else:
                self.manager.resume("manual")
        data = event.data
        if event.kind == "news":
            assert isinstance(data, NewsEvent)
            self.news.setdefault(f"{data.id}:{data.received_at.isoformat()}", data)
            # Keep one day plus a margin, preventing unbounded feed memory.
            self.news = {k: v for k, v in self.news.items() if (now - v.received_at).total_seconds() <= 172800}
            if self.repository:
                self.repository.record("news_events", {**jsonable(data), "classification": jsonable(classify(data))})
        elif event.kind == "snapshot":
            assert isinstance(data, StockSnapshot)
            self.snapshots[data.quote.symbol] = data
            if self.repository:
                self.repository.record("stocks", {"id": data.quote.symbol, "symbol": data.quote.symbol, **jsonable(data)})
            self._on_quote(data.quote, now)
        elif event.kind == "quote":
            assert isinstance(data, Quote)
            self._on_quote(data, now)
        elif event.kind == "bar":
            assert isinstance(data, Bar)
            history = self.bars.setdefault(data.symbol, [])
            # Ignore duplicated/revised bars; never trade twice on the same close.
            if history and data.timestamp <= history[-1].timestamp:
                return
            history.append(data)
            del history[:-400]
            if self.repository:
                self.repository.record("market_bars", data)
            snapshot = self.snapshots.get(data.symbol)
            if snapshot:
                accepted, reasons = scan(snapshot, now, self.settings)
                if accepted:
                    active_symbols = sorted(self.snapshots, key=lambda s: self.snapshots[s].dollar_volume, reverse=True)[:self.settings.max_candidates]
                    if data.symbol in active_symbols:
                        signal = self.strategy.evaluate(StrategyContext(bars=history, snapshot=snapshot,
                            news=list(self.news.values()), now=now, settings=self.settings, analyses=self.analyses))
                        if signal:
                            self.signal_count += 1
                            if self.repository:
                                self.repository.record("signals", signal)
                            order = self.manager.submit(signal, snapshot.quote, now)
                            if order is None:
                                self.rejected_signals += 1
                elif self.repository:
                    self.repository.log("INFO", "scanner excluded", symbol=data.symbol, reasons=reasons,
                                        event_time=now)
        else:
            raise ValueError(f"unsupported event {event.kind}")
        if event.kind in ("quote", "snapshot"):
            self.equity_curve.append({"timestamp": now.isoformat(), "equity": self.manager.account.equity,
                                      "strategy": self.strategy_name})
            if self.live:
                del self.equity_curve[:-10000]
        self.persist()

    def _on_quote(self, quote: Quote, now: datetime):
        # All held positions need fresh marks before another entry can fill.
        # Checking only this tick misses a stale holding in a five-symbol book.
        def fresh(value: Quote) -> bool:
            return (utc(value.timestamp) <= utc(value.received_at) <= now
                    and (now - utc(value.timestamp)).total_seconds() <= self.settings.max_quote_age_seconds
                    and (now - utc(value.received_at)).total_seconds() <= self.settings.max_quote_age_seconds)

        observed = dict(self.quotes)
        if fresh(quote):
            previous = observed.get(quote.symbol)
            if previous is None or quote.timestamp >= previous.timestamp:
                observed[quote.symbol] = quote
        if not fresh(quote) or any(symbol not in observed or not fresh(observed[symbol])
                                   for symbol in self.manager.account.positions):
            self.manager.halt("stale_market_data")
        else:
            self.manager.resume("stale_market_data")
        self.manager.on_quote(quote, now)
        if (utc(quote.timestamp) > utc(quote.received_at) or utc(quote.received_at) > now
                or (now - utc(quote.timestamp)).total_seconds() > self.settings.max_quote_age_seconds
                or (now - utc(quote.received_at)).total_seconds() > self.settings.max_quote_age_seconds):
            self.manager.halt("stale_market_data")
            if self.repository:
                self.repository.record("quote_snapshots", quote)
            return
        previous = self.quotes.get(quote.symbol)
        if previous and quote.timestamp < previous.timestamp:
            return
        self.quotes[quote.symbol] = quote
        if all(symbol in self.quotes and (now - utc(self.quotes[symbol].timestamp)).total_seconds() <= self.settings.max_quote_age_seconds
               for symbol in self.manager.account.positions):
            self.manager.resume("stale_market_data")
        self.manager.mark_to_market(self.quotes)
        self.manager.check_exits(quote, now)
        if self.repository:
            self.repository.record("quote_snapshots", quote)

    def persist(self):
        if not self.repository:
            return
        with self.repository.transaction():
            self.repository.save_state(self.manager.export_state())
            for order in self.manager.orders.values():
                payload = jsonable(order)
                if self._journal_orders.get(order.client_id) != payload:
                    self.repository.record("orders", order)
                    self._journal_orders[order.client_id] = payload
            for fill in self.manager.fills:
                if fill.id not in self._journal_fills:
                    self.repository.record("executions", fill)
                    self._journal_fills.add(fill.id)
            for alert in getattr(self.manager, "alerts", []):
                if alert["id"] not in self._journal_alerts:
                    self.repository.record("alerts", alert)
                    self._journal_alerts.add(alert["id"])
            self.repository.replace_positions(self.manager.account.positions)
            if self.last_time:
                account = self.manager.account
                self.repository.record("daily_performance", {"id": str(self._session), "date": str(self._session),
                    "equity": account.equity, "cash": account.cash,
                    "realized_pnl": account.realized_pnl, "total_pnl": account.equity - account.day_start_equity,
                    "entries": account.entries_today, "timestamp": self.last_time, "strategy": self.strategy_name})
            self.repository.set_control("engine_context", {"strategy": self.strategy_name,
                "last_time": self.last_time, "bars": [bar for values in self.bars.values() for bar in values],
                "snapshots": list(self.snapshots.values()), "news": list(self.news.values()),
                "analyses": self.analyses,
                "signal_count": self.signal_count, "rejected_signals": self.rejected_signals})
            execution_mode = getattr(self.manager, "execution_mode", "paper")
            self.repository.set_control("runtime", {"mode": execution_mode, "strategy": self.strategy_name,
                "last_event": self.last_time, "halt_reasons": sorted(self.manager.halt_reasons)})
