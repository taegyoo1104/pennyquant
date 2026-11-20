"""실제 주문 API를 호출하지 않는 소수점 체결·잔고 모의 장부."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from .models import (ZERO, QUANTITY_STEP, AccountView, FractionalFill, FractionalOrder,
                     FractionalPosition, OrderIntent, PortfolioSettings, aware, decimal, serialize)

OPEN_STATUSES = {'submitted', 'accepted', 'open', 'partial', 'cancel_pending', 'unknown'}


class PaperBroker:
    """금액 매수와 수량 매도를 동일한 체결 장부로 처리한다.

    기본 시장가 근사는 접수 지연 후 관측된 호가로 체결한다. 기존 가격제한
    방식은 명시적 실험 모드로 보존한다. 실제 증권사 매칭을 재현하지 않는다.
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
        self.quote_sequences: dict[str, str] = {}
        self.last_event_at: datetime | None = None

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
        if self.session_date is not None and day<self.session_date:
            raise ValueError('과거 거래일로 장부를 되돌려 손실 제한을 초기화할 수 없습니다.')
        if self.session_date != day:
            self.session_date = day
            self.day_start_equity = self.equity
            self.halted = False
            for order in self.get_open_orders():
                if order.intent.side == 'buy':
                    self.cancel_order(order.client_id, now)

    def check_daily_loss(self, now: datetime) -> bool:
        self.roll_session(now)
        if self.equity <= self.day_start_equity * (1 - self.settings.daily_loss_limit):
            self.halted = True
            for order in self.get_open_orders():
                if order.intent.side == 'buy':
                    self.cancel_order(order.client_id, now)
            self._alert('daily_loss_limit', now, '일일 손실 한도에 도달하여 당일 신규 매수를 중단했습니다.')
        return self.halted

    def submit_order(self, intent: OrderIntent, now: datetime | None = None) -> FractionalOrder:
        if not isinstance(intent, OrderIntent):
            raise ValueError('검증된 OrderIntent가 필요합니다.')
        previous = self.orders.get(intent.client_id)
        if previous is not None:
            if serialize(previous.intent) != serialize(intent):
                raise ValueError('같은 주문 ID를 다른 주문 내용으로 재사용할 수 없습니다.')
            return previous
        submitted = aware(now or intent.created_at)
        if now is not None and (submitted < intent.created_at or (self.last_event_at and submitted < self.last_event_at)):
            raise ValueError('제출 시각은 의도 생성·장부 확인 시각보다 과거일 수 없습니다.')
        submitted = max(submitted,self.last_event_at or submitted)
        self.roll_session(submitted)
        legacy = self.settings.paper_fill_model == 'legacy_limit_v1'
        order = FractionalOrder(intent,status='open' if legacy else 'submitted',
            fill_model_version=self.settings.paper_fill_model,submitted_at=submitted,
            accepted_at=submitted if legacy else None,expected_price=intent.expected_price or intent.limit_price,
            expected_price_source='decision_quote' if intent.expected_price is not None else 'intent_reference',
            assumed_latency=ZERO if legacy else self.settings.paper_latency_seconds,
            fee_breakdown=self.settings.fee_breakdown(intent.side,ZERO),unfilled_reason='awaiting_quote' if legacy else 'awaiting_acceptance')
        self._transition(order,order.status,submitted,force=True)
        reason = ''
        if not legacy:
            from .session import SessionExitManager
            schedule = SessionExitManager(self).schedule(submitted)
            if not schedule or not schedule.opens_at <= submitted < schedule.fractional_deadline:
                reason = 'outside_fractional_order_window'
            elif intent.side == 'buy' and intent.amount != intent.amount.quantize(Decimal('0.01')):
                reason = 'amount_step_unsupported'
            elif intent.side == 'sell' and intent.qty != intent.qty.quantize(QUANTITY_STEP):
                reason = 'quantity_step_unsupported'
        if reason:
            pass
        elif intent.side == 'buy':
            if any(o.status == 'unknown' for o in self.get_open_orders()):
                reason = 'account_order_state_unknown'
            elif self.check_daily_loss(submitted):
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
            order.rejection_reason = reason
            order.unfilled_reason = reason
            self._transition(order,'rejected',submitted)
        self.orders[intent.client_id] = order
        return order

    def _transition(self, order, status, now, *, force=False, event=None):
        """모의 상태 전이를 기록한다. 접수·취소 확인은 실제 브로커 응답이 아니다."""
        changed = order.status != status
        order.status = status
        if changed or force:
            order.lifecycle.append({'status':status,'event':event or status,'observed_at':aware(now).isoformat(),
                'source':'paper_assumption','fill_model_version':order.fill_model_version})

    def cancel_order(self, client_id: str, now: datetime | None = None) -> FractionalOrder | None:
        order = self.orders.get(client_id)
        if order is None or order.status not in OPEN_STATUSES:
            return order
        stamp = aware(now or self.last_event_at or order.submitted_at or order.intent.created_at)
        if order.status == 'unknown':
            if order.cancel_requested_at is None:
                order.cancel_requested_at = stamp
                self._transition(order,'unknown',stamp,force=True,event='cancel_requested_unconfirmed')
            order.unfilled_reason = 'cancel_result_unknown'
            return order
        if order.cancel_effective_at is not None:
            return order
        order.cancel_requested_at = stamp
        delay = ZERO if self.settings.paper_fill_model == 'legacy_limit_v1' else self.settings.paper_cancel_latency_seconds
        order.cancel_effective_at = stamp+timedelta(microseconds=int(delay*1_000_000))
        self._transition(order,'cancel_pending',stamp)
        order.unfilled_reason = 'cancel_pending'
        if delay == 0:
            self._transition(order,'cancelled',stamp)
            order.unfilled_reason = 'cancelled'
        return order

    def mark_unknown(self, client_id: str, now: datetime, reason='simulated_result_unknown'):
        """장애 시나리오용 미확인 상태. 시세로 접수·체결을 추측해 복구하지 않는다."""
        order = self.orders[client_id]
        if order.status not in OPEN_STATUSES:
            raise ValueError('종료 주문을 미확인 진행 주문으로 되돌릴 수 없습니다.')
        order.unfilled_reason = reason
        self._transition(order,'unknown',now)
        self._alert('paper_order_unknown',now,'모의 주문 결과가 미확인입니다. 예약을 유지하고 자동 체결·재전송하지 않습니다.',order.intent.symbol)
        return order

    def expire_orders(self, now: datetime):
        now = aware(now)
        market = self.settings.paper_fill_model == 'market_v2'
        if market:
            if self.last_event_at and now < self.last_event_at:
                return
            self.last_event_at = now
        active = list(self.get_open_orders())
        schedule = None
        if market and active:
            from .session import SessionExitManager
            schedule = SessionExitManager(self).schedule(now)
        for order in active:
            if order.status == 'unknown':
                continue
            submitted = order.submitted_at or order.intent.created_at
            if market:
                ny = ZoneInfo('America/New_York')
                if (now.astimezone(ny).date() > submitted.astimezone(ny).date()
                        or (schedule and now >= schedule.closes_at)):
                    self._transition(order,'cancelled',now,event='day_expired')
                    order.unfilled_reason = 'day_expired'
                    continue
                due = submitted+timedelta(microseconds=int((order.assumed_latency or ZERO)*1_000_000))
                if order.accepted_at is None and now >= due:
                    order.accepted_at = due
                    if order.status == 'submitted':
                        self._transition(order,'accepted',now)
                        order.unfilled_reason = 'awaiting_post_acceptance_quote'
                if order.cancel_effective_at is not None and order.cancel_effective_at <= now:
                    self._transition(order,'cancelled',now)
                    order.unfilled_reason = 'cancelled'
                    continue
            if (now-submitted).total_seconds() >= self.settings.order_ttl_seconds:
                self.cancel_order(order.client_id,now)
                if order.status == 'cancelled':
                    order.unfilled_reason = 'assumed_order_timeout'

    def quote_is_fresh(self, quote, now: datetime) -> bool:
        try:
            now, ts, received = aware(now), aware(quote.timestamp), aware(quote.received_at)
            bid, ask = decimal(quote.bid, positive=True), decimal(quote.ask, positive=True)
            return (ts <= received <= now and (now - ts).total_seconds() <= self.settings.max_quote_age_seconds
                    and (now - received).total_seconds() <= self.settings.max_quote_age_seconds and bid <= ask
                    and not quote.halted and quote.bid_size > 0 and quote.ask_size > 0)
        except (AttributeError, ValueError, TypeError):
            return False

    def process_quote(self, quote, now: datetime | None = None, *, max_fill_qty: Decimal | None = None,
                      paper_tradable: bool | None = None, buy_fill_guard=None) -> list[FractionalFill]:
        if paper_tradable is not None and type(paper_tradable) is not bool:
            raise ValueError('Paper 거래 허용 값은 불리언이어야 합니다.')
        if paper_tradable is not None and self.settings.paper_fill_model != 'market_v2':
            raise ValueError('Paper 조건 분리는 market_v2 체결 모형에서만 지원합니다.')
        if self.settings.paper_fill_model == 'legacy_limit_v1':
            from .legacy_fills import process_legacy_quote
            return process_legacy_quote(self,quote,now,max_fill_qty=max_fill_qty,buy_fill_guard=buy_fill_guard)
        from .market_fills import process_market_quote
        return process_market_quote(self,quote,now,max_fill_qty=max_fill_qty,paper_tradable=paper_tradable,
                                    buy_fill_guard=buy_fill_guard)

    def _fill_record(self, order, qty, price, fee, now, pnl, quote):
        """주문 누적값과 체결별 비교 근거를 동일한 형식으로 기록한다."""
        expected = order.expected_price
        sign = Decimal(1) if order.intent.side == 'buy' else Decimal(-1)
        bps = (price/expected-1)*10000*sign if expected else None
        order.simulated_fill_price = order.filled_notional/order.filled_qty
        order.slippage_bps = (order.simulated_fill_price/expected-1)*10000*sign if expected else None
        current = self.settings.fee_breakdown(order.intent.side,order.filled_notional)
        prior = self.settings.fee_breakdown(order.intent.side,order.filled_notional-qty*price)
        breakdown = {k:current[k]-prior[k] for k in current}
        order.fee_breakdown = current
        order.unfilled_reason = '' if order.status == 'filled' else 'liquidity_partial'
        self._transition(order,order.status,now,force=True,event='fill')
        delay = Decimal(str((aware(now)-(order.submitted_at or order.intent.created_at)).total_seconds()))
        return FractionalFill(f'{order.client_id}:{len(self.fills)+1}',order.client_id,order.intent.symbol,
            order.intent.side,qty,price,fee,now,pnl,order.fill_model_version,expected,price,
            order.assumed_latency,bps,breakdown,aware(quote.timestamp),aware(quote.received_at),delay,
            reason=order.intent.reason)

    def reconcile(self) -> dict:
        issues = []
        if self.cash < 0 or self.reserved_cash > self.cash:
            issues.append('cash_or_reservation_mismatch')
        if any(p.qty <= 0 or p.avg_price <= 0 or p.entry_fee < 0 for p in self.positions.values()):
            issues.append('invalid_position')
        if len({f.id for f in self.fills}) != len(self.fills):
            issues.append('duplicate_fill_id')
        for order in self.orders.values():
            if (order.status not in OPEN_STATUSES|{'filled','cancelled','rejected'}
                    or min(order.filled_qty,order.filled_notional,order.fee) < 0
                    or (order.intent.amount is not None and order.filled_notional > order.intent.amount)
                    or (order.intent.qty is not None and order.filled_qty > order.intent.qty)):
                issues.append('invalid_order_totals_or_state')
            if self.settings.paper_fill_model == 'market_v2':
                if (order.fill_model_version != 'market_v2' or order.submitted_at is None
                        or order.submitted_at < order.intent.created_at or order.assumed_latency is None
                        or order.assumed_latency < 0 or (order.accepted_at is not None and order.accepted_at < order.submitted_at)):
                    issues.append('invalid_execution_model_metadata')
        if self.settings.paper_fill_model == 'market_v2' and any(fill.fill_model_version != 'market_v2' for fill in self.fills):
            issues.append('mixed_fill_model_versions')
        for symbol, position in self.positions.items():
            reserved = sum((o.remaining_qty for o in self.get_open_orders(symbol) if o.intent.side == 'sell'), ZERO)
            if reserved > position.qty:
                issues.append('oversold_reservation')
        return {'ok': not issues, 'mode': 'paper', 'issues': issues, 'account': serialize(self.get_account())}

    def to_state(self) -> dict:
        return serialize({'version': 2, 'mode': 'paper', 'settings': self.settings, 'cash': self.cash,
                          'equity': self.equity, 'day_start_equity': self.day_start_equity,
                          'reserved_cash': self.reserved_cash, 'realized_pnl': self.realized_pnl,
                          'session_date': self.session_date, 'halted': self.halted,
                          'orders': self.orders, 'positions': self.positions, 'fills': self.fills,
                          'exit_intents': self.exit_intents, 'alerts': self.alerts, 'quote_watermarks': self.quote_watermarks, 'quote_sequences':self.quote_sequences, 'last_event_at':self.last_event_at})

    @classmethod
    def from_state(cls, state: dict, settings: PortfolioSettings | None = None) -> 'PaperBroker':
        if state.get('mode') != 'paper' or state.get('version') not in {1,2}:
            raise ValueError('지원하지 않는 모의 장부 형식입니다.')
        stored_values = dict(state['settings'])
        if state['version'] == 1:
            stored_values['paper_fill_model'] = 'legacy_limit_v1'
        stored_settings = PortfolioSettings(**stored_values)
        if state['version'] == 1 and settings is not None and settings.paper_fill_model != 'legacy_limit_v1':
            raise ValueError('이전 가격제한 장부입니다. portfolio.paper_fill_model: legacy_limit_v1로 복구하거나 새 db_path에서 market_v2를 시작하세요.')
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
            values = {k:v for k,v in data.items() if k != 'intent'}
            for name in ('filled_qty','filled_notional','fee','expected_price','simulated_fill_price','assumed_latency','slippage_bps'):
                if values.get(name) is not None:
                    values[name] = decimal(values[name],name,nonnegative=name != 'slippage_bps')
            for name in ('submitted_at','accepted_at','cancel_requested_at','cancel_effective_at'):
                if values.get(name) is not None:
                    values[name] = aware(values[name])
            values['fee_breakdown'] = {k:decimal(v,nonnegative=True) if v is not None else None for k,v in values.get('fee_breakdown',{}).items()}
            result.orders[key] = FractionalOrder(intent,**values)
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
            for name in ('qty','price','fee','realized_pnl','expected_price','simulated_fill_price','assumed_latency','slippage_bps','observed_latency'):
                if value.get(name) is not None:
                    value[name] = decimal(value[name],name)
            for name in ('quote_timestamp','quote_received_at'):
                if value.get(name) is not None:
                    value[name] = aware(value[name])
            value['fee_breakdown'] = {k:decimal(v) if v is not None else None for k,v in value.get('fee_breakdown',{}).items()}
            value['timestamp'] = aware(value['timestamp'])
            result.fills.append(FractionalFill(**value))
        result.exit_intents = deepcopy(state.get('exit_intents', {}))
        result.alerts = deepcopy(state.get('alerts', []))
        result.quote_watermarks = deepcopy(state.get('quote_watermarks', {}))
        result.quote_sequences = deepcopy(state.get('quote_sequences',{}))
        result.last_event_at = aware(state['last_event_at']) if state.get('last_event_at') else None
        if not result.reconcile()['ok']:
            raise ValueError('모의 장부 대사에 실패하여 복구를 중단했습니다.')
        return result
