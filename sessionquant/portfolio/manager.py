"""시장·손실·비중 조건을 결합해 주문 의도를 생성하는 규칙 엔진."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256

from .broker import PaperBroker, OPEN_STATUSES
from .models import ZERO, CENT, QUANTITY_STEP, AllocationDecision, OrderIntent, aware, decimal, serialize


class PortfolioManager:
    def __init__(self, broker: PaperBroker, exit_policy=None):
        from .exit_policy import ExitPolicy
        self.broker = broker
        self.settings = broker.settings
        self.exit_policy = exit_policy or ExitPolicy()

    def plan_entry(self, symbol: str, ask, stop_price, now: datetime, *, market_state: str = 'UNKNOWN',
                   tradable: bool = False, fractional_supported: bool = False, trend_confirmed: bool = False,
                   support_confirmed: bool = False, rebound_confirmed: bool = False, major_bad_news: bool = False,
                   add: bool = False, signal_id: str = '', session_allowed: bool = True,
                   strategy: str = 'pullback', bid=None) -> AllocationDecision:
        """전체 수량·예약·비용을 다시 계산한다. 예상 손실은 시장가 갭의 보장 상한이 아니다."""
        from .session import SessionExitManager
        now = aware(now)
        ask,stop = decimal(ask,'ask',positive=True),decimal(stop_price,'stop_price',positive=True)
        mark = decimal(bid,'bid',positive=True) if bid is not None else ask
        self.broker.roll_session(now)
        audit = {'checked_at':now.isoformat(),'estimate_only':True,'bid_verified_by_caller':bid is not None,
                 'requested_stop':str(stop),'market_state':market_state,'is_add':add}
        reject = lambda reason: AllocationDecision(False,reason,audit=serialize(audit))
        flags = (tradable,fractional_supported,trend_confirmed,support_confirmed,rebound_confirmed,major_bad_news,add,session_allowed)
        if any(type(value) is not bool for value in flags) or mark > ask:
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
        if any(o.status == 'unknown' for o in open_orders):
            return reject('account_order_state_unknown')
        if any(o.intent.symbol == symbol for o in open_orders) or symbol in self.broker.exit_intents:
            return reject('pending_order_or_exit')
        if add:
            if (not position or not self.settings.allow_adds or position.adds >= self.settings.max_adds
                    or not support_confirmed or not rebound_confirmed):
                return reject('add_conditions_not_met')
            if ask <= position.avg_price:
                return reject('add_would_average_down')
            stop = max(position.stop_price,stop)
            if stop >= ask:
                return reject('invalid_add_stop')
        elif position:
            return reject('position_exists')
        snapshot = self.risk_snapshot(now)
        audit['before'] = snapshot
        if snapshot['issues']:
            return reject('existing_risk_limit_breached')
        exposed = set(self.broker.positions)|{o.intent.symbol for o in open_orders if o.intent.side == 'buy'}
        if symbol not in exposed and len(exposed) >= self.settings.max_positions:
            return reject('position_limit')
        buys = [o for o in open_orders if o.intent.side == 'buy']
        if any(o.intent.reference_bid is None for o in buys):
            return reject('pending_mark_reference_unknown')
        equity = self.broker.equity
        symbol_value = position.qty*(position.mark_price or position.avg_price) if position else ZERO
        gross = sum((p.qty*(p.mark_price or p.avg_price) for p in self.broker.positions.values()),ZERO)
        pending = sum((o.remaining_amount for o in buys),ZERO)
        pending_fees = sum((max(ZERO,self.settings.fees('buy',o.intent.amount)-o.fee) for o in buys),ZERO)
        pending_mark_loss = sum((o.remaining_amount*max(ZERO,1-o.intent.reference_bid/o.intent.limit_price) for o in buys),ZERO)
        cash = self.broker.get_available_cash()
        cap = min(equity*(self.settings.add_weight if add else self.settings.initial_weight),cash,
                  equity*self.settings.max_symbol_weight-symbol_value,equity*self.settings.max_gross_weight-gross-pending)
        if cap < self.settings.minimum_order:
            return reject('allocation_or_cash_limit')
        price = ask*(1+self.settings.slippage_rate)
        sell_price = stop*(1-self.settings.slippage_rate)

        def projection(amount):
            qty = (amount/price).quantize(QUANTITY_STEP,rounding=ROUND_DOWN)
            entry_fee = self.settings.fees('buy',amount)
            new_value = qty*mark
            projected_equity = equity-pending_fees-pending_mark_loss-entry_fee-(amount-new_value)
            old_qty = position.qty if position else ZERO
            old_basis = position.cost_basis if position else ZERO
            proceeds = (old_qty+qty)*sell_price
            loss = max(ZERO,old_basis+amount+entry_fee-proceeds+self.settings.fees('sell',proceeds))
            return {'amount':amount,'planned_qty':qty,'planned_execution_price':price,'stop_price':stop,
                'old_qty':old_qty,'old_basis':old_basis,'combined_qty':old_qty+qty,'entry_fee':entry_fee,
                'exit_fee_estimate':self.settings.fees('sell',proceeds),'stop_loss_estimate':loss,
                'risk_budget':max(ZERO,projected_equity*self.settings.risk_per_symbol),
                'projected_equity':projected_equity,'projected_symbol_value':symbol_value+new_value,
                'projected_gross_value':gross+pending+new_value,'pending_amount':pending,
                'pending_fees':pending_fees,'pending_mark_loss_estimate':pending_mark_loss,
                'available_cash':cash,'open_position_and_buy_symbols':len(exposed|{symbol})}

        def permissible(amount):
            row = projection(amount)
            return (row['projected_equity'] > 0 and row['stop_loss_estimate'] <= row['risk_budget']
                and row['projected_symbol_value'] <= row['projected_equity']*self.settings.max_symbol_weight
                and row['projected_gross_value'] <= row['projected_equity']*self.settings.max_gross_weight
                and amount+row['entry_fee'] <= cash)

        low,high = ZERO,cap
        for _ in range(90):
            mid = (low+high)/2
            if permissible(mid):low=mid
            else:high=mid
        amount = (cap if permissible(cap) else low).quantize(CENT,rounding=ROUND_DOWN)
        audit['projection'] = projection(amount)
        if amount < self.settings.minimum_order or not permissible(amount):
            return reject('risk_budget_insufficient')
        seed = f'{symbol}|{signal_id}|{aware(now).isoformat()}|{add}'
        client_id = 'sq-buy-'+sha256(seed.encode()).hexdigest()[:32]
        intent = OrderIntent(client_id,symbol,'buy',now,price,amount=amount,stop_price=stop,strategy=strategy,
            reason='추세 재확인 추가매수' if add else '기술적 추세와 시장 강세 확인',is_add=add,
            expected_price=ask,reference_bid=mark if bid is not None else None)
        return AllocationDecision(True,'approved',intent,serialize(audit))

    def risk_snapshot(self, now):
        """현재 모의 장부의 예상 위험을 보고하고 갭·비용으로 넘은 예산을 알린다."""
        equity = self.broker.equity
        positions = self.broker.positions
        buys = [o for o in self.broker.get_open_orders() if o.intent.side == 'buy']
        pending = sum((o.remaining_amount for o in buys),ZERO)
        gross = sum((p.qty*(p.mark_price or p.avg_price) for p in positions.values()),ZERO)
        issues,rows = [],{}
        if equity <= 0:issues.append('account_equity_nonpositive')
        if gross+pending > equity*self.settings.max_gross_weight:issues.append('gross_exposure_exceeded')
        if len(set(positions)|{o.intent.symbol for o in buys}) > self.settings.max_positions:issues.append('position_count_exceeded')
        for symbol,p in positions.items():
            proceeds = p.qty*min(p.stop_price,p.mark_price or p.avg_price)*(1-self.settings.slippage_rate)
            loss = max(ZERO,p.cost_basis-proceeds+self.settings.fees('sell',proceeds))
            value = p.qty*(p.mark_price or p.avg_price)
            local = []
            if loss > equity*self.settings.risk_per_symbol:local.append('symbol_risk_budget_exceeded')
            if value > equity*self.settings.max_symbol_weight:local.append('symbol_weight_exceeded')
            rows[symbol] = {'qty':p.qty,'stop_price':p.stop_price,'loss_estimate':loss,
                            'risk_budget':equity*self.settings.risk_per_symbol,'market_value':value,'issues':local}
            issues.extend(f'{symbol}:{code}' for code in local)
            for code in local:self.broker._alert(code,now,'모의 체결 후 예상 위험 예산을 초과했습니다. 갭·비용으로 계획 손실을 넘을 수 있습니다.',symbol)
        for code in [i for i in issues if ':' not in i]:self.broker._alert(code,now,'모의 장부의 포지션·전체 노출 한도를 초과했습니다. 신규 진입을 보류합니다.')
        return serialize({'checked_at':aware(now),'basis':'paper_ledger_marks','estimate_only':True,
            'equity':equity,'gross_value':gross,'pending_buy_amount':pending,'reserved_cash':self.broker.reserved_cash,
            'positions':rows,'issues':issues,'status':'BREACHED' if issues else 'WITHIN_ESTIMATES'})

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
                self.broker.cancel_order(order.client_id,now)
        self.broker.expire_orders(now)
        if any(o.intent.side == 'buy' for o in self.broker.get_open_orders(symbol)):
            self.broker._alert('entry_cancel_unconfirmed',now,'매수 취소가 확인되지 않아 잔량 매도를 보류합니다.',symbol)
            return None
        # 취소 전 부분 체결이 있었을 수 있으므로 확정 후 실제 잔량을 다시 읽는다.
        position = self.broker.positions.get(symbol)
        if position is None:
            self.broker.exit_intents.pop(symbol,None)
            return None
        if any(o.intent.side == 'sell' for o in self.broker.get_open_orders(symbol)):
            return None
        from .session import SessionExitManager
        schedule = SessionExitManager(self.broker).schedule(now)
        if not schedule or not schedule.opens_at <= now < schedule.fractional_deadline:
            self.broker._alert('exit_order_window_closed',now,'청산 접수 가능 시간을 벗어났습니다. 잔량을 유지하고 다음 조치를 확인하세요.',symbol)
            return None
        if not self.broker.quote_is_fresh(quote, now):
            return None
        latch['attempt'] += 1
        seed = f'{symbol}|{latch["opened_at"]}|{latch["requested_at"]}|{latch["attempt"]}'
        client_id = 'sq-exit-' + sha256(seed.encode()).hexdigest()[:32]
        limit = decimal(quote.bid) * (1 - self.settings.slippage_rate)
        intent = OrderIntent(client_id, symbol, 'sell', now, limit, qty=position.qty,
                             strategy=position.strategy, reason=latch['reason'],expected_price=decimal(quote.bid))
        return self.broker.submit_order(intent)

    def check_exits(self, quote, now: datetime, *, bearish_signal: bool = False,
                    confirmed_adverse: bool = False, symbol=None) -> list:
        """10% 도달 전에도 확인된 악재나 수익 중 하락 신호에 대응한다."""
        now = aware(now)
        symbol = symbol or getattr(quote,'symbol',None)
        position = self.broker.positions.get(symbol)
        if position is None:
            return []
        reason = self.broker.exit_intents.get(symbol, {}).get('reason')
        if reason is None and confirmed_adverse is True:
            reason = 'confirmed_adverse_news'
        if reason is None and self.broker.halted:
            reason = 'daily_loss_limit'
        if reason is None and self.exit_policy.time_due(position.opened_at,now):
            reason = 'holding_time_exit'
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
            elif self.exit_policy.profit_protection and bearish_signal is True and net > 0:
                reason = 'profit_protection'
            elif self.broker.halted:
                reason = 'daily_loss_limit'
        if reason:
            order = self._queue_exit(symbol, quote, now, reason)
            return [order] if order is not None else []
        return []
