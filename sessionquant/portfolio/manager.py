"""시장·손실·비중 조건을 결합해 주문 의도를 생성하는 규칙 엔진."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256

from .broker import PaperBroker, OPEN_STATUSES
from .models import ZERO, CENT, AllocationDecision, OrderIntent, aware, decimal


class PortfolioManager:
    def __init__(self, broker: PaperBroker):
        self.broker = broker
        self.settings = broker.settings

    def plan_entry(self, symbol: str, ask, stop_price, now: datetime, *, market_state: str = 'UNKNOWN',
                   tradable: bool = False, fractional_supported: bool = False, trend_confirmed: bool = False,
                   support_confirmed: bool = False, rebound_confirmed: bool = False, major_bad_news: bool = False,
                   add: bool = False, signal_id: str = '', session_allowed: bool = True,
                   strategy: str = 'pullback') -> AllocationDecision:
        """손절 시 전체 종목의 예상 비용 포함 손실이 위험 예산을 넘지 않게 한다."""
        from .session import SessionExitManager
        now = aware(now)
        ask, stop = decimal(ask, 'ask', positive=True), decimal(stop_price, 'stop_price', positive=True)
        self.broker.roll_session(now)
        reject = lambda reason: AllocationDecision(False, reason)
        flags = (tradable, fractional_supported, trend_confirmed, support_confirmed, rebound_confirmed,
                 major_bad_news, add, session_allowed)
        if any(type(value) is not bool for value in flags):
            return reject('invalid_evidence')
        if self.broker.check_daily_loss(now):
            return reject('daily_loss_limit')
        if not session_allowed or not SessionExitManager(self.broker).permits_entry(now):
            return reject('outside_entry_window')
        if market_state != 'BULLISH':
            return reject('market_not_bullish')
        if not tradable or not fractional_supported:
            return reject('eligibility_unverified')
        if major_bad_news:
            return reject('confirmed_company_risk')
        if not trend_confirmed:
            return reject('trend_unconfirmed')
        if stop >= ask:
            return reject('invalid_stop')
        position = self.broker.positions.get(symbol)
        open_orders = self.broker.get_open_orders()
        if any(o.intent.symbol == symbol for o in open_orders) or symbol in self.broker.exit_intents:
            return reject('pending_order_or_exit')
        if add:
            if (not position or not self.settings.allow_adds or position.adds >= self.settings.max_adds
                    or not support_confirmed or not rebound_confirmed):
                return reject('add_conditions_not_met')
            stop = max(position.stop_price, stop)
            if stop >= ask:
                return reject('invalid_add_stop')
        elif position:
            return reject('position_exists')
        exposed_symbols = set(self.broker.positions) | {o.intent.symbol for o in open_orders if o.intent.side == 'buy'}
        if symbol not in exposed_symbols and len(exposed_symbols) >= self.settings.max_positions:
            return reject('position_limit')
        equity = self.broker.equity
        symbol_value = position.qty * (position.mark_price or position.avg_price) if position else ZERO
        gross = sum((p.qty * (p.mark_price or p.avg_price) for p in self.broker.positions.values()), ZERO)
        pending = sum((o.remaining_amount for o in open_orders if o.intent.side == 'buy'), ZERO)
        cash = self.broker.get_available_cash()
        cap = min(equity * (self.settings.add_weight if add else self.settings.initial_weight), cash,
                  equity * self.settings.max_symbol_weight - symbol_value,
                  equity * self.settings.max_gross_weight - gross - pending)
        if cap < self.settings.minimum_order:
            return reject('allocation_or_cash_limit')
        price = ask * (1 + self.settings.slippage_rate)
        sell_price = stop * (1 - self.settings.slippage_rate)
        budget = equity * self.settings.risk_per_symbol

        def permissible(amount: Decimal):
            qty = amount / price
            old_qty = position.qty if position else ZERO
            old_basis = position.cost_basis if position else ZERO
            basis = old_basis + amount + self.settings.fees('buy', amount)
            proceeds = (old_qty + qty) * sell_price
            loss = max(ZERO, basis - proceeds + self.settings.fees('sell', proceeds))
            return loss <= budget and amount + self.settings.fees('buy', amount) <= cash

        low, high = ZERO, cap
        for _ in range(90):
            mid = (low + high) / 2
            if permissible(mid):
                low = mid
            else:
                high = mid
        amount = (cap if permissible(cap) else low).quantize(CENT, rounding=ROUND_DOWN)
        if amount < self.settings.minimum_order or not permissible(amount):
            return reject('risk_budget_insufficient')
        seed = f'{symbol}|{signal_id}|{aware(now).isoformat()}|{add}'
        client_id = 'sq-buy-' + sha256(seed.encode()).hexdigest()[:32]
        intent = OrderIntent(client_id, symbol, 'buy', now, price, amount=amount, stop_price=stop,
                             strategy=strategy, reason='추세 재확인 추가매수' if add else '기술적 추세와 시장 강세 확인', is_add=add)
        return AllocationDecision(True, 'approved', intent)

    def _queue_exit(self, symbol: str, quote, now: datetime, reason: str):
        position = self.broker.positions.get(symbol)
        if position is None:
            self.broker.exit_intents.pop(symbol, None)
            return None
        latch = self.broker.exit_intents.setdefault(symbol, {'reason': reason, 'opened_at': position.opened_at.isoformat(),
                                                            'requested_at': now.isoformat(), 'attempt': 0})
        if latch['opened_at'] != position.opened_at.isoformat():
            raise ValueError('청산 의도와 현재 보유 포지션의 생성 시각이 다릅니다.')
        for order in self.broker.get_open_orders(symbol):
            if order.intent.side == 'buy':
                self.broker.cancel_order(order.client_id)
        self.broker.expire_orders(now)
        if any(o.intent.side == 'sell' for o in self.broker.get_open_orders(symbol)):
            return None
        if not self.broker.quote_is_fresh(quote, now):
            return None
        latch['attempt'] += 1
        seed = f'{symbol}|{latch["opened_at"]}|{latch["requested_at"]}|{latch["attempt"]}'
        client_id = 'sq-exit-' + sha256(seed.encode()).hexdigest()[:32]
        limit = decimal(quote.bid) * (1 - self.settings.slippage_rate)
        intent = OrderIntent(client_id, symbol, 'sell', now, limit, qty=position.qty,
                             strategy=position.strategy, reason=latch['reason'])
        return self.broker.submit_order(intent)

    def check_exits(self, quote, now: datetime, *, bearish_signal: bool = False,
                    confirmed_adverse: bool = False) -> list:
        """10% 도달 전에도 확인된 악재나 수익 중 하락 신호에 대응한다."""
        now = aware(now)
        position = self.broker.positions.get(quote.symbol)
        if position is None:
            return []
        reason = self.broker.exit_intents.get(quote.symbol, {}).get('reason')
        if reason is None and self.broker.quote_is_fresh(quote, now):
            bid = decimal(quote.bid)
            proceeds = position.qty * bid * (1 - self.settings.slippage_rate)
            net = proceeds - self.settings.fees('sell', proceeds) - position.cost_basis
            if confirmed_adverse is True:
                reason = 'confirmed_adverse_news'
            elif bid <= position.stop_price:
                reason = 'stop_loss'
            elif net >= position.cost_basis * self.settings.target_return:
                reason = 'net_profit_target'
            elif bearish_signal is True and net > 0:
                reason = 'profit_protection'
            elif self.broker.halted:
                reason = 'daily_loss_limit'
        if reason:
            order = self._queue_exit(quote.symbol, quote, now, reason)
            return [order] if order is not None else []
        return []
