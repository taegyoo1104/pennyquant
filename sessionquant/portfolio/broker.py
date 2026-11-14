"""실제 주문 API를 호출하지 않는 소수점 체결·잔고 모의 장부."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from .models import (ZERO, QUANTITY_STEP, AccountView, FractionalFill, FractionalOrder,
                     FractionalPosition, OrderIntent, PortfolioSettings, aware, decimal, serialize)

OPEN_STATUSES = {'open', 'partial', 'unknown'}


class PaperBroker:
    """금액 매수와 수량 매도를 동일한 체결 장부로 처리한다.

    제출은 체결이 아니다. 제출 이후 수신한 유효한 호가가 제한 가격과
    유동성을 충족할 때만 체결하며, 슬리피지와 주문 전체 수수료를 적용한다.
    """
    mode = 'paper'

    def __init__(self, settings: PortfolioSettings | None = None):
        self.settings = settings or PortfolioSettings()
        self.cash = self.settings.initial_cash
        self.day_start_equity = self.cash
        self.realized_pnl = ZERO
        self.session_date: str | None = None
        self.halted = False
        self.orders: dict[str, FractionalOrder] = {}
        self.positions: dict[str, FractionalPosition] = {}
        self.fills: list[FractionalFill] = []
        self.exit_intents: dict[str, dict] = {}
        self.alerts: list[dict] = []
        self.quote_watermarks: dict[str, str] = {}

    @property
    def equity(self) -> Decimal:
        return self.cash + sum((p.qty * (p.mark_price or p.avg_price) for p in self.positions.values()), ZERO)

    @property
    def reserved_cash(self) -> Decimal:
        reserved = ZERO
        for order in self.get_open_orders():
            if order.intent.side == 'buy':
                total_fee = self.settings.fees('buy', order.intent.amount)
                reserved += order.remaining_amount + max(ZERO, total_fee - order.fee)
        return reserved

    def get_available_cash(self) -> Decimal:
        return max(ZERO, self.cash - self.reserved_cash)

    def get_account(self) -> AccountView:
        return AccountView(self.cash, self.equity, self.day_start_equity, self.reserved_cash,
                           self.realized_pnl, deepcopy(self.positions), self.halted)

    def get_positions(self) -> dict[str, FractionalPosition]:
        return deepcopy(self.positions)

    def get_order(self, client_id: str) -> FractionalOrder | None:
        return self.orders.get(client_id)

    def get_open_orders(self, symbol: str | None = None) -> list[FractionalOrder]:
        return [o for o in self.orders.values() if o.status in OPEN_STATUSES and (symbol is None or o.intent.symbol == symbol)]

    def _alert(self, code: str, now: datetime, message: str, symbol: str | None = None):
        key = f'{code}:{self.session_date}:{symbol or "account"}'
        if not any(a['id'] == key for a in self.alerts):
            self.alerts.append({'id': key, 'code': code, 'timestamp': aware(now).isoformat(),
                                'message': message, 'symbol': symbol, 'severity': 'critical'})

    def roll_session(self, now: datetime):
        day = aware(now).astimezone(ZoneInfo('America/New_York')).date().isoformat()
        if self.session_date != day:
            self.session_date = day
            self.day_start_equity = self.equity
            self.halted = False
            for order in self.get_open_orders():
                if order.intent.side == 'buy':
                    self.cancel_order(order.client_id)

    def check_daily_loss(self, now: datetime) -> bool:
        self.roll_session(now)
        if self.equity <= self.day_start_equity * (1 - self.settings.daily_loss_limit):
            self.halted = True
            for order in self.get_open_orders():
                if order.intent.side == 'buy':
                    self.cancel_order(order.client_id)
            self._alert('daily_loss_limit', now, '일일 손실 한도에 도달하여 당일 신규 매수를 중단했습니다.')
        return self.halted

    def submit_order(self, intent: OrderIntent) -> FractionalOrder:
        if not isinstance(intent, OrderIntent):
            raise ValueError('검증된 OrderIntent가 필요합니다.')
        previous = self.orders.get(intent.client_id)
        if previous is not None:
            if serialize(previous.intent) != serialize(intent):
                raise ValueError('같은 주문 ID를 다른 주문 내용으로 재사용할 수 없습니다.')
            return previous
        self.roll_session(intent.created_at)
        order = FractionalOrder(intent)
        reason = ''
        if intent.side == 'buy':
            if self.check_daily_loss(intent.created_at):
                reason = 'daily_loss_limit'
            elif intent.amount < self.settings.minimum_order:
                reason = 'below_minimum_order'
            elif intent.amount + self.settings.fees('buy', intent.amount) > self.get_available_cash():
                reason = 'insufficient_cash'
            elif any(o.intent.side == 'buy' for o in self.get_open_orders(intent.symbol)):
                reason = 'pending_buy_exists'
            elif any(o.intent.side == 'sell' for o in self.get_open_orders(intent.symbol)) or intent.symbol in self.exit_intents:
                reason = 'exit_pending'
            elif intent.is_add and (intent.symbol not in self.positions or not self.settings.allow_adds
                    or self.positions[intent.symbol].adds >= self.settings.max_adds):
                reason = 'add_not_allowed'
            elif not intent.is_add and intent.symbol in self.positions:
                reason = 'position_exists'
            else:
                symbols = set(self.positions) | {o.intent.symbol for o in self.get_open_orders() if o.intent.side == 'buy'}
                if intent.symbol not in symbols and len(symbols) >= self.settings.max_positions:
                    reason = 'position_limit'
        else:
            position = self.positions.get(intent.symbol)
            reserved = sum((o.remaining_qty for o in self.get_open_orders(intent.symbol) if o.intent.side == 'sell'), ZERO)
            if position is None or intent.qty > position.qty - reserved:
                reason = 'insufficient_sellable_quantity'
        if reason:
            order.status, order.rejection_reason = 'rejected', reason
        self.orders[intent.client_id] = order
        return order

    def cancel_order(self, client_id: str) -> FractionalOrder | None:
        order = self.orders.get(client_id)
        if order is not None and order.status in OPEN_STATUSES:
            order.status = 'cancelled'
        return order

    def expire_orders(self, now: datetime):
        now = aware(now)
        for order in self.get_open_orders():
            if (now - order.intent.created_at).total_seconds() >= self.settings.order_ttl_seconds:
                self.cancel_order(order.client_id)

    def quote_is_fresh(self, quote, now: datetime) -> bool:
        try:
            now, ts, received = aware(now), aware(quote.timestamp), aware(quote.received_at)
            bid, ask = decimal(quote.bid, positive=True), decimal(quote.ask, positive=True)
            return (ts <= received <= now and (now - ts).total_seconds() <= self.settings.max_quote_age_seconds
                    and (now - received).total_seconds() <= self.settings.max_quote_age_seconds and bid <= ask
                    and not quote.halted and quote.bid_size > 0 and quote.ask_size > 0)
        except (AttributeError, ValueError, TypeError):
            return False

    def process_quote(self, quote, now: datetime | None = None, *, max_fill_qty: Decimal | None = None) -> list[FractionalFill]:
        now = aware(now or quote.received_at)
        self.roll_session(now)
        self.expire_orders(now)
        if not self.quote_is_fresh(quote, now):
            return []
        from .session import SessionExitManager
        schedule = SessionExitManager(self).schedule(now)
        if schedule is None or not schedule.opens_at <= now < schedule.fractional_deadline:
            return []
        bid, ask = decimal(quote.bid), decimal(quote.ask)
        if quote.symbol in self.positions:
            self.positions[quote.symbol].mark_price = bid
        self.check_daily_loss(now)
        fills = []
        fingerprint = '|'.join(str(v) for v in (aware(quote.timestamp).isoformat(), quote.bid, quote.ask, quote.bid_size, quote.ask_size))
        if self.quote_watermarks.get(quote.symbol) == fingerprint:
            return fills
        self.quote_watermarks[quote.symbol] = fingerprint
        liquidity = decimal(max_fill_qty, 'max_fill_qty', nonnegative=True) if max_fill_qty is not None else None
        for order in list(self.get_open_orders(quote.symbol)):
            intent = order.intent
            if intent.created_at > aware(quote.timestamp) or intent.created_at > now:
                continue
            side = intent.side
            if side == 'buy':
                from ..session_window import permits_new_entry
                if not permits_new_entry(now,min_entry_to_exit_minutes=self.settings.min_entry_to_exit_minutes):
                    self.cancel_order(order.client_id)
                    continue
            price = ask * (1 + self.settings.slippage_rate) if side == 'buy' else bid * (1 - self.settings.slippage_rate)
            if (side == 'buy' and price > intent.limit_price) or (side == 'sell' and price < intent.limit_price):
                continue
            if side == 'buy' and (self.halted or not quote.tradable):
                continue
            remaining = order.remaining_amount / price if side == 'buy' else order.remaining_qty
            available = decimal(quote.ask_size if side == 'buy' else quote.bid_size)
            if liquidity is not None:
                available = min(available, liquidity)
            qty = min(remaining, available).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
            if qty <= 0:
                continue
            notional = qty * price
            fee = self.settings.fees(side, order.filled_notional + notional) - order.fee
            if side == 'buy' and notional + fee > self.cash:
                continue
            pnl = ZERO
            if side == 'buy':
                position = self.positions.get(intent.symbol)
                if position is None:
                    position = FractionalPosition(intent.symbol, ZERO, price, ZERO, intent.stop_price, now, intent.strategy)
                    self.positions[intent.symbol] = position
                if intent.is_add and order.filled_qty == 0:
                    position.adds += 1
                position.avg_price = (position.qty * position.avg_price + notional) / (position.qty + qty)
                position.qty += qty
                position.entry_fee += fee
                # 추가매수는 이전 손절선을 내릴 수 없다.
                position.stop_price = max(position.stop_price, intent.stop_price)
                position.mark_price = bid
                if intent.is_add:
                    position.add_amount += notional
                else:
                    position.initial_amount += notional
                self.cash -= notional + fee
            else:
                position = self.positions.get(intent.symbol)
                if position is None or qty > position.qty:
                    self.cancel_order(order.client_id)
                    continue
                entry_fee = position.entry_fee * qty / position.qty
                pnl = notional - fee - position.avg_price * qty - entry_fee
                self.cash += notional - fee
                self.realized_pnl += pnl
                position.qty -= qty
                position.entry_fee -= entry_fee
                if position.qty == 0:
                    del self.positions[intent.symbol]
                    self.exit_intents.pop(intent.symbol, None)
            order.filled_qty += qty
            order.filled_notional += notional
            order.fee += fee
            complete = order.remaining_amount < price * QUANTITY_STEP if side == 'buy' else order.remaining_qty == 0
            order.status = 'filled' if complete else 'partial'
            fill = FractionalFill(f'{order.client_id}:{len(self.fills)+1}', order.client_id, intent.symbol, side,
                                  qty, price, fee, now, pnl)
            self.fills.append(fill)
            fills.append(fill)
            if liquidity is not None:
                liquidity -= qty
            self.check_daily_loss(now)
        return fills

    def reconcile(self) -> dict:
        issues = []
        if self.cash < 0 or self.reserved_cash > self.cash:
            issues.append('cash_or_reservation_mismatch')
        if any(p.qty <= 0 or p.avg_price <= 0 or p.entry_fee < 0 for p in self.positions.values()):
            issues.append('invalid_position')
        if len({f.id for f in self.fills}) != len(self.fills):
            issues.append('duplicate_fill_id')
        for symbol, position in self.positions.items():
            reserved = sum((o.remaining_qty for o in self.get_open_orders(symbol) if o.intent.side == 'sell'), ZERO)
            if reserved > position.qty:
                issues.append('oversold_reservation')
        return {'ok': not issues, 'mode': 'paper', 'issues': issues, 'account': serialize(self.get_account())}

    def to_state(self) -> dict:
        return serialize({'version': 1, 'mode': 'paper', 'settings': self.settings, 'cash': self.cash,
                          'equity': self.equity, 'day_start_equity': self.day_start_equity,
                          'reserved_cash': self.reserved_cash, 'realized_pnl': self.realized_pnl,
                          'session_date': self.session_date, 'halted': self.halted,
                          'orders': self.orders, 'positions': self.positions, 'fills': self.fills,
                          'exit_intents': self.exit_intents, 'alerts': self.alerts, 'quote_watermarks': self.quote_watermarks})

    @classmethod
    def from_state(cls, state: dict, settings: PortfolioSettings | None = None) -> 'PaperBroker':
        if state.get('mode') != 'paper' or state.get('version') != 1:
            raise ValueError('지원하지 않는 모의 장부 형식입니다.')
        stored_settings = PortfolioSettings(**state['settings'])
        if settings is not None and serialize(settings) != serialize(stored_settings):
            raise ValueError('재시작 설정이 저장된 위험·비용 설정과 다릅니다. 별도 모의 실행 공간을 사용하세요.')
        result = cls(stored_settings)
        for name in ('cash', 'day_start_equity', 'realized_pnl'):
            setattr(result, name, decimal(state[name], name))
        result.session_date = state.get('session_date')
        if type(state['halted']) is not bool:
            raise ValueError('저장된 위험 중지 상태가 올바르지 않습니다.')
        result.halted = state['halted']
        for key, data in state.get('orders', {}).items():
            intent = OrderIntent(**data['intent'])
            if key != intent.client_id:
                raise ValueError('저장된 주문 ID가 일치하지 않습니다.')
            result.orders[key] = FractionalOrder(intent, data['status'], decimal(data['filled_qty'], nonnegative=True),
                decimal(data['filled_notional'], nonnegative=True), decimal(data['fee'], nonnegative=True), data.get('rejection_reason', ''))
        for symbol, data in state.get('positions', {}).items():
            value = dict(data)
            for name in ('qty', 'avg_price', 'entry_fee', 'stop_price', 'initial_amount', 'add_amount', 'mark_price'):
                value[name] = decimal(value[name], name, nonnegative=True)
            value['opened_at'] = aware(value['opened_at'])
            if value['symbol'] != symbol or type(value['adds']) is not int or not 0 <= value['adds'] <= 1:
                raise ValueError('저장된 보유 종목 정보가 올바르지 않습니다.')
            result.positions[symbol] = FractionalPosition(**value)
        for data in state.get('fills', []):
            value = dict(data)
            for name in ('qty', 'price', 'fee', 'realized_pnl'):
                value[name] = decimal(value[name], name)
            value['timestamp'] = aware(value['timestamp'])
            result.fills.append(FractionalFill(**value))
        result.exit_intents = deepcopy(state.get('exit_intents', {}))
        result.alerts = deepcopy(state.get('alerts', []))
        result.quote_watermarks = deepcopy(state.get('quote_watermarks', {}))
        if not result.reconcile()['ok']:
            raise ValueError('모의 장부 대사에 실패하여 복구를 중단했습니다.')
        return result
