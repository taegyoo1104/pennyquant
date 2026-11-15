"""공개 호가만 사용하는 소수점 시장가 근사. 실제 매칭·지연을 보장하지 않는다."""
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from .models import ZERO, QUANTITY_STEP, FractionalPosition, aware, decimal


def process_market_quote(broker, quote, now: datetime | None = None, *, max_fill_qty=None, paper_tradable=None):
    now = aware(now or quote.received_at)
    if broker.last_event_at and now < broker.last_event_at:
        return []
    broker.roll_session(now)
    broker.expire_orders(now)

    def unfilled(reason):
        for order in broker.get_open_orders(quote.symbol):
            if order.status not in {'unknown','cancel_pending'}:
                order.unfilled_reason = reason
        return []

    if not broker.quote_is_fresh(quote,now):
        return unfilled('quote_invalid_stale_halted_or_missing_depth')
    from .session import SessionExitManager
    schedule = SessionExitManager(broker).schedule(now)
    stamp = aware(quote.timestamp)
    previous = broker.quote_sequences.get(quote.symbol)
    if previous and stamp <= aware(previous):
        return unfilled('duplicate_or_out_of_order_quote')
    broker.quote_sequences[quote.symbol] = stamp.isoformat()
    bid,ask = decimal(quote.bid),decimal(quote.ask)
    if quote.symbol in broker.positions:
        broker.positions[quote.symbol].mark_price = bid
    broker.check_daily_loss(now)
    # A closed order window prevents execution, not valuation of residual shares.
    if schedule is None or not schedule.opens_at <= now < schedule.fractional_deadline:
        return unfilled('outside_fractional_execution_window')
    participation = broker.settings.paper_depth_participation
    depths = {'buy':decimal(quote.ask_size)*participation,'sell':decimal(quote.bid_size)*participation}
    liquidity = decimal(max_fill_qty,'max_fill_qty',nonnegative=True) if max_fill_qty is not None else None
    fills = []
    for order in list(broker.get_open_orders(quote.symbol)):
        intent = order.intent
        if order.status == 'unknown':
            continue
        submitted = order.submitted_at or intent.created_at
        if order.accepted_at is None or stamp <= submitted or stamp < order.accepted_at:
            order.unfilled_reason = 'awaiting_post_acceptance_quote'
            continue
        if order.status not in {'accepted','partial','cancel_pending'}:
            continue
        side = intent.side
        if side == 'buy':
            from ..session_window import permits_new_entry
            if not permits_new_entry(now,min_entry_to_exit_minutes=broker.settings.min_entry_to_exit_minutes):
                broker.cancel_order(order.client_id,now)
                continue
            # 별도 Paper 정책은 호출자가 재검사한다. 원본 quote.tradable은 변경하지 않는다.
            if broker.halted or not (quote.tradable if paper_tradable is None else paper_tradable):
                order.unfilled_reason = 'buy_halted_or_untradable'
                continue
        # 시장가에는 내부 예상 가격을 상한/하한으로 적용하지 않는다.
        price = ask*(1+broker.settings.slippage_rate) if side == 'buy' else bid*(1-broker.settings.slippage_rate)
        remaining = order.remaining_amount/price if side == 'buy' else order.remaining_qty
        available = depths[side] if liquidity is None else min(depths[side],liquidity)
        qty = min(remaining,available).quantize(QUANTITY_STEP,rounding=ROUND_DOWN)
        if qty <= 0:
            order.unfilled_reason = 'liquidity_or_quantity_step_insufficient'
            continue
        notional = qty*price
        fee = broker.settings.fees(side,order.filled_notional+notional)-order.fee
        if side == 'buy':
            this_reserve = order.remaining_amount+max(ZERO,broker.settings.fees('buy',intent.amount)-order.fee)
            others = broker.reserved_cash-this_reserve
            if notional+fee > broker.cash-others:
                order.unfilled_reason = 'insufficient_unreserved_cash'
                continue
        pnl = ZERO
        if side == 'buy':
            position = broker.positions.get(intent.symbol)
            if position is not None and intent.is_add and price < position.avg_price:
                broker._alert('add_execution_below_average',now,'추가매수 주문 후 가격 갭으로 평균가보다 낮게 체결되었습니다. 체결을 취소로 숨기지 않고 위험을 재점검합니다.',intent.symbol)
            if position is None:
                position = FractionalPosition(intent.symbol,ZERO,price,ZERO,intent.stop_price,now,intent.strategy)
                broker.positions[intent.symbol] = position
            if intent.is_add and order.filled_qty == 0:
                position.adds += 1
            position.avg_price = (position.qty*position.avg_price+notional)/(position.qty+qty)
            position.qty += qty
            position.entry_fee += fee
            position.stop_price = max(position.stop_price,intent.stop_price)
            position.mark_price = bid
            if intent.is_add:
                position.add_amount += notional
            else:
                position.initial_amount += notional
            broker.cash -= notional+fee
        else:
            position = broker.positions.get(intent.symbol)
            if position is None or qty > position.qty:
                broker.cancel_order(order.client_id,now)
                continue
            entry_fee = position.entry_fee*qty/position.qty
            pnl = notional-fee-position.avg_price*qty-entry_fee
            broker.cash += notional-fee
            broker.realized_pnl += pnl
            position.qty -= qty
            position.entry_fee -= entry_fee
            if position.qty == 0:
                del broker.positions[intent.symbol]
                broker.exit_intents.pop(intent.symbol,None)
        order.filled_qty += qty
        order.filled_notional += notional
        order.fee += fee
        complete = order.remaining_amount < price*QUANTITY_STEP if side == 'buy' else order.remaining_qty == 0
        broker._transition(order,'filled' if complete else 'partial',now)
        fill = broker._fill_record(order,qty,price,fee,now,pnl,quote)
        broker.fills.append(fill)
        fills.append(fill)
        depths[side] -= qty
        if liquidity is not None:
            liquidity -= qty
        broker.check_daily_loss(now)
    return fills
