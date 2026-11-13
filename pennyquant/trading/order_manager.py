"""Deterministic whole-share paper execution with durable, replay-safe state.

This is a quote/depth estimate, not an exchange matching engine. Stops are local
triggers producing limits; a gap or halt can leave them unfilled. A quote used to
create an order can never fill that order. The caller persists export_state()
atomically with its event records after each event.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import hashlib
import math
from typing import Any
from zoneinfo import ZoneInfo

from ..config.settings import Settings
from ..models import Account, Fill, Order, OrderStatus, Position, Quote, Signal, jsonable, utc
from .risk_manager import RiskManager, is_regular_session

ACTIVE = {OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.UNKNOWN}
KNOWN_ACTIVE = ACTIVE - {OrderStatus.UNKNOWN}
DAILY_HALTS = {"daily_loss", "consecutive_losses", "entry_limit"}
NY = ZoneInfo("America/New_York")


class PaperOrderManager:
    def __init__(self, settings: Settings, account: Account, repository=None):
        self.settings = settings
        self.account = account
        self.repository = repository  # caller owns transaction boundaries
        self.risk = RiskManager(settings)
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.closed_trades: list[dict[str, Any]] = []
        self.halt_reasons: set[str] = set()
        self.last_rejection: str | None = None
        self._day: str | None = None
        self._signal_fingerprints: dict[str, str] = {}
        self._quote_liquidity: dict[str, dict[str, int]] = {}
        self._latest_quotes: dict[str, Quote] = {}
        self._trade_totals: dict[str, dict[str, Any]] = {}

    def _refresh_reservations(self) -> None:
        self.account.reserved_cash = sum(
            (order.qty - order.filled_qty) * order.limit_price * (1 + self.settings.fee_bps / 10_000)
            for order in self.orders.values()
            if order.side == "buy" and order.status in ACTIVE
        )

    def halt(self, reason: str) -> None:
        self.halt_reasons.add(reason)
        # Cancelling known resting buys also prevents new fills after the stop.
        # UNKNOWN cannot safely be cancelled without reconciliation.
        for order in self.orders.values():
            if order.side == "buy" and order.status in KNOWN_ACTIVE:
                order.status = OrderStatus.CANCELLED
        self._refresh_reservations()

    def resume(self, reason: str) -> bool:
        if reason == "unknown_order" and any(o.status == OrderStatus.UNKNOWN for o in self.orders.values()):
            return False
        self.halt_reasons.discard(reason)
        return reason not in self.halt_reasons

    def roll_day(self, now: datetime) -> None:
        day = utc(now).astimezone(NY).date().isoformat()
        if self._day is None:
            self._day = day
            return
        if day == self._day:
            return
        self._day = day
        self.account.day_start_equity = self.account.equity
        self.account.realized_pnl = 0.0
        self.account.entries_today = 0
        self.account.consecutive_losses = 0
        self.halt_reasons.difference_update(DAILY_HALTS)
        # Orders in this MVP are DAY orders, with no overnight resting entry.
        for order in self.orders.values():
            if order.status in KNOWN_ACTIVE:
                order.status = OrderStatus.CANCELLED
        self._refresh_reservations()

    @staticmethod
    def _fingerprint(signal: Signal) -> str:
        return repr((signal.symbol, signal.strategy, utc(signal.timestamp).isoformat(), signal.reference_price))

    def submit(self, signal: Signal, quote: Quote, now: datetime) -> Order | None:
        now = utc(now)
        self.roll_day(now)
        client_id = "pq-" + hashlib.sha256(("buy:" + signal.id).encode()).hexdigest()[:28]
        if client_id in self.orders:
            if self._signal_fingerprints.get(signal.id) != self._fingerprint(signal):
                self.last_rejection = "idempotency_conflict"
                self.halt("idempotency_conflict")
                return None
            return self.orders[client_id]
        if any(order.status == OrderStatus.UNKNOWN for order in self.orders.values()):
            self.halt("unknown_order")
        if any(order.side == "buy" and order.status in ACTIVE for order in self.orders.values()):
            self.last_rejection = "pending_buy"
            return None
        decision = self.risk.evaluate(signal, quote, self.account, now, halted=bool(self.halt_reasons))
        if not decision.approved:
            self.last_rejection = decision.reason
            return None
        order = Order(client_id, signal.symbol, "buy", decision.qty, decision.limit_price,
                      now, OrderStatus.OPEN, strategy=signal.strategy, reason=signal.reason)
        self.orders[client_id] = order
        self._signal_fingerprints[signal.id] = self._fingerprint(signal)
        # Accepted entries consume the daily limit even if they expire unfilled.
        self.account.entries_today += 1
        self._refresh_reservations()
        self.last_rejection = None
        return order

    def cancel(self, client_id: str, now: datetime) -> bool:
        utc(now)
        order = self.orders.get(client_id)
        if order is None or order.status not in ACTIVE:
            return False
        if order.status == OrderStatus.UNKNOWN:
            self.halt("unknown_order")
            return False
        order.status = OrderStatus.CANCELLED
        self._refresh_reservations()
        return True

    def mark_unknown(self, client_id: str) -> None:
        self.orders[client_id].status = OrderStatus.UNKNOWN
        self.halt("unknown_order")

    def _valid_quote(self, quote: Quote, now: datetime) -> bool:
        now = utc(now)
        if any(not isinstance(size, int) or isinstance(size, bool) or size < 0
               for size in (quote.bid_size, quote.ask_size)):
            return False
        timestamp, receipt = utc(quote.timestamp), utc(quote.received_at)
        if timestamp > receipt or receipt > now:
            return False
        if (now - timestamp).total_seconds() > self.settings.max_quote_age_seconds:
            return False
        if (now - receipt).total_seconds() > self.settings.max_quote_age_seconds:
            return False
        if not quote.tradable or quote.halted:
            return False
        if self.settings.regular_hours_only and not is_regular_session(now):
            return False
        previous = self._latest_quotes.get(quote.symbol)
        return previous is None or timestamp >= utc(previous.timestamp)

    def on_quote(self, quote: Quote, now: datetime) -> list[Fill]:
        now = utc(now)
        self.roll_day(now)
        for order in self.orders.values():
            if order.status in KNOWN_ACTIVE and (now - utc(order.created_at)).total_seconds() >= self.settings.order_ttl_seconds:
                self.cancel(order.client_id, now)
        if not self._valid_quote(quote, now):
            return []
        self._latest_quotes[quote.symbol] = quote
        # Same market timestamp is one depth budget, even after reconnect/replay.
        key = quote.symbol + "|" + utc(quote.timestamp).isoformat()
        # Older market timestamps are rejected by _valid_quote. Only the newest
        # budget per symbol can ever be reused, so retaining older ticks would
        # grow every persisted engine snapshot throughout a live session.
        self._quote_liquidity = {
            saved_key: budget for saved_key, budget in self._quote_liquidity.items()
            if saved_key.rsplit("|", 1)[0] != quote.symbol or saved_key == key
        }
        capacity = self._quote_liquidity.setdefault(key, {
            "buy_used": 0, "sell_used": 0,
            "ask_size": quote.ask_size, "bid_size": quote.bid_size,
        })
        made: list[Fill] = []
        fee_rate = self.settings.fee_bps / 10_000
        slip = self.settings.slippage_bps / 10_000
        for order in sorted(self.orders.values(), key=lambda o: (o.created_at, o.client_id)):
            if order.symbol != quote.symbol or order.status not in KNOWN_ACTIVE:
                continue
            # Strictly later market AND receipt event, not merely later wall time.
            if utc(quote.timestamp) <= utc(order.created_at) or utc(quote.received_at) <= utc(order.created_at):
                continue
            if order.side == "buy":
                if self.halt_reasons:
                    self.cancel(order.client_id, now)
                    continue
                price = round(quote.ask * (1 + slip), 8)
                if price > order.limit_price + 1e-9:
                    continue
                depth = max(0, min(capacity["ask_size"], quote.ask_size) - capacity["buy_used"])
                affordable = math.floor((self.account.cash + 1e-9) / (price * (1 + fee_rate)))
                qty = min(order.qty - order.filled_qty, depth, affordable)
            else:
                price = round(quote.bid * (1 - slip), 8)
                if price + 1e-9 < order.limit_price:
                    continue
                position = self.account.positions.get(order.symbol)
                if position is None:
                    self.cancel(order.client_id, now)
                    continue
                depth = max(0, min(capacity["bid_size"], quote.bid_size) - capacity["sell_used"])
                qty = min(order.qty - order.filled_qty, depth, position.qty)
            if qty <= 0:
                continue
            fee = round(price * qty * fee_rate, 10)
            fill = Fill(f"{order.client_id}-{order.filled_qty + qty}", order.client_id,
                        order.symbol, order.side, qty, price, fee, now)
            self._book(order, fill)
            capacity[f"{order.side}_used"] += qty
            made.append(fill)
        self._refresh_reservations()
        self.mark_to_market(self._latest_quotes)
        return made

    def _book(self, order: Order, fill: Fill) -> None:
        before = order.filled_qty
        order.filled_qty += fill.qty
        order.avg_fill_price = ((order.avg_fill_price * before + fill.price * fill.qty) / order.filled_qty)
        order.status = OrderStatus.FILLED if order.filled_qty == order.qty else OrderStatus.PARTIAL
        self.fills.append(fill)
        if fill.side == "buy":
            self.account.cash = round(self.account.cash - fill.price * fill.qty - fill.fee, 10)
            position = self.account.positions.get(fill.symbol)
            if position is None:
                position = Position(fill.symbol, fill.qty, fill.price, fill.fee,
                                    fill.price * (1 - self.settings.stop_pct),
                                    fill.price * (1 + self.settings.take_profit_pct), fill.timestamp, order.strategy)
                self.account.positions[fill.symbol] = position
                self._trade_totals[fill.symbol] = {
                    "symbol": fill.symbol, "strategy": order.strategy,
                    "entry_time": utc(fill.timestamp).isoformat(), "entry_qty": 0,
                    "entry_notional": 0.0, "entry_fee": 0.0,
                    "exit_qty": 0, "exit_notional": 0.0, "exit_fee": 0.0, "net_pnl": 0.0,
                }
            else:
                total = position.qty + fill.qty
                position.avg_price = (position.avg_price * position.qty + fill.price * fill.qty) / total
                position.qty = total
                position.entry_fee += fill.fee
                position.stop_price = position.avg_price * (1 - self.settings.stop_pct)
                position.target_price = position.avg_price * (1 + self.settings.take_profit_pct)
            trade = self._trade_totals[fill.symbol]
            trade["entry_qty"] += fill.qty
            trade["entry_notional"] += fill.qty * fill.price
            trade["entry_fee"] += fill.fee
        else:
            position = self.account.positions[fill.symbol]
            allocated_fee = position.entry_fee * fill.qty / position.qty
            pnl = (fill.price - position.avg_price) * fill.qty - fill.fee - allocated_fee
            self.account.cash = round(self.account.cash + fill.price * fill.qty - fill.fee, 10)
            self.account.realized_pnl += pnl
            position.qty -= fill.qty
            position.entry_fee -= allocated_fee
            trade = self._trade_totals.setdefault(fill.symbol, {
                "symbol": fill.symbol, "strategy": position.strategy,
                "entry_time": utc(position.opened_at).isoformat(),
                "entry_qty": position.qty + fill.qty,
                "entry_notional": position.avg_price * (position.qty + fill.qty),
                "entry_fee": position.entry_fee + allocated_fee,
                "exit_qty": 0, "exit_notional": 0.0, "exit_fee": 0.0, "net_pnl": 0.0,
            })
            trade["exit_qty"] += fill.qty
            trade["exit_notional"] += fill.qty * fill.price
            trade["exit_fee"] += fill.fee
            trade["net_pnl"] += pnl
            if position.qty == 0:
                del self.account.positions[fill.symbol]
                completed = self._trade_totals.pop(fill.symbol)
                completed.update({
                    "exit_time": utc(fill.timestamp).isoformat(),
                    "qty": completed["exit_qty"],
                    "entry_price": completed["entry_notional"] / completed["entry_qty"],
                    "exit_price": completed["exit_notional"] / completed["exit_qty"],
                    "reason": order.reason,
                })
                completed["return_pct"] = completed["net_pnl"] / completed["entry_notional"] * 100
                self.closed_trades.append(completed)
                if completed["net_pnl"] < 0:
                    self.account.consecutive_losses += 1
                else:
                    self.account.consecutive_losses = 0

    def mark_to_market(self, quotes: dict[str, Quote]) -> None:
        self._latest_quotes.update(quotes)
        self.account.equity = self.account.cash + sum(
            position.qty * (quotes.get(symbol) or self._latest_quotes.get(symbol)).bid
            if (quotes.get(symbol) or self._latest_quotes.get(symbol)) is not None
            else position.qty * position.avg_price
            for symbol, position in self.account.positions.items()
        )
        if self.account.day_start_equity > 0 and self.account.equity <= self.account.day_start_equity * (1 - self.settings.daily_loss_pct) + 1e-10:
            self.halt("daily_loss")
        if self.account.consecutive_losses >= self.settings.max_consecutive_losses:
            self.halt("consecutive_losses")

    def check_exits(self, quote: Quote, now: datetime) -> Order | None:
        now = utc(now)
        if not self._valid_quote(quote, now):
            return None
        position = self.account.positions.get(quote.symbol)
        if position is None:
            return None
        for order in self.orders.values():
            if order.symbol == quote.symbol and order.side == "sell" and order.status in ACTIVE:
                return order
        reason = "stop_loss" if quote.bid <= position.stop_price else "take_profit" if quote.bid >= position.target_price else None
        if reason is None:
            return None
        # Prevent an unfilled remainder from reopening exposure after exit.
        for order in self.orders.values():
            if order.symbol == quote.symbol and order.side == "buy" and order.status in KNOWN_ACTIVE:
                self.cancel(order.client_id, now)
        sequence = sum(o.side == "sell" and o.symbol == quote.symbol for o in self.orders.values())
        identity = f"sell:{quote.symbol}:{utc(position.opened_at).isoformat()}:{sequence}"
        client_id = "pq-" + hashlib.sha256(identity.encode()).hexdigest()[:28]
        price = quote.bid * (1 - self.settings.slippage_bps / 10_000)
        limit = max(0.01, math.floor((price + 1e-10) * 100) / 100)
        order = Order(client_id, quote.symbol, "sell", position.qty, limit, now,
                      OrderStatus.OPEN, strategy=position.strategy, reason=reason)
        self.orders[client_id] = order
        return order

    def reconcile(self, positions: dict[str, int], open_order_ids: set[str]) -> bool:
        expected_positions = {symbol: p.qty for symbol, p in self.account.positions.items()}
        expected_orders = {o.client_id for o in self.orders.values() if o.status in ACTIVE}
        supplied_positions = {s: q for s, q in positions.items() if q}
        if expected_positions != supplied_positions or expected_orders != set(open_order_ids):
            self.halt("reconciliation_mismatch")
            return False
        if any(order.status == OrderStatus.UNKNOWN for order in self.orders.values()):
            self.halt("unknown_order")
            return False
        self.halt_reasons.discard("reconciliation_mismatch")
        return True

    def export_state(self) -> dict[str, Any]:
        return jsonable({
            "version": 1, "settings": self.settings, "account": self.account, "orders": list(self.orders.values()),
            "fills": self.fills, "closed_trades": self.closed_trades,
            "halt_reasons": sorted(self.halt_reasons), "last_rejection": self.last_rejection,
            "day": self._day, "signal_fingerprints": self._signal_fingerprints,
            "quote_liquidity": self._quote_liquidity, "latest_quotes": self._latest_quotes,
            "trade_totals": self._trade_totals,
        })

    def restore_state(self, state: dict[str, Any]) -> None:
        if state.get("version") != 1:
            raise ValueError("unsupported order state version")
        if state.get("settings") != asdict(self.settings):
            raise ValueError("persisted settings differ; use a fresh database for new parameters")
        raw_account = dict(state["account"])
        positions: dict[str, Position] = {}
        for symbol, raw_position in raw_account.pop("positions", {}).items():
            item = dict(raw_position)
            item["opened_at"] = utc(item["opened_at"])
            position = Position(**item)
            if position.symbol != symbol or not isinstance(position.qty, int) or position.qty <= 0:
                raise ValueError("invalid restored position")
            for value in (position.avg_price, position.stop_price, position.target_price):
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid restored price")
            positions[symbol] = position
        account = Account(**raw_account, positions=positions)
        if not math.isfinite(account.cash) or account.cash < -1e-8:
            raise ValueError("invalid restored cash")
        orders: dict[str, Order] = {}
        for raw_order in state.get("orders", []):
            item = dict(raw_order)
            item["created_at"] = utc(item["created_at"])
            item["status"] = OrderStatus(item["status"])
            order = Order(**item)
            if (not isinstance(order.qty, int) or order.qty <= 0 or not 0 <= order.filled_qty <= order.qty
                    or not math.isfinite(order.limit_price) or order.limit_price <= 0 or order.side not in {"buy", "sell"}
                    or order.client_id in orders):
                raise ValueError("invalid restored order")
            orders[order.client_id] = order
        fills = []
        for raw_fill in state.get("fills", []):
            item = dict(raw_fill)
            item["timestamp"] = utc(item["timestamp"])
            fills.append(Fill(**item))
        self.account = account
        self.orders = orders
        self.fills = fills
        self.closed_trades = list(state.get("closed_trades", []))
        self.halt_reasons = set(state.get("halt_reasons", []))
        self.last_rejection = state.get("last_rejection")
        self._day = state.get("day")
        self._signal_fingerprints = dict(state.get("signal_fingerprints", {}))
        self._quote_liquidity = dict(state.get("quote_liquidity", {}))
        self._trade_totals = dict(state.get("trade_totals", {}))
        self._latest_quotes = {}
        for symbol, raw_quote in state.get("latest_quotes", {}).items():
            item = dict(raw_quote)
            item["timestamp"], item["received_at"] = utc(item["timestamp"]), utc(item["received_at"])
            self._latest_quotes[symbol] = Quote(**item)
        latest_keys = {symbol + "|" + utc(quote.timestamp).isoformat()
                       for symbol, quote in self._latest_quotes.items()}
        self._quote_liquidity = {key: value for key, value in self._quote_liquidity.items()
                                 if key in latest_keys}
        self._refresh_reservations()
        if any(order.status == OrderStatus.UNKNOWN for order in self.orders.values()):
            self.halt("unknown_order")
