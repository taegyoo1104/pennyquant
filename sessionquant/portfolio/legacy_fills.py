"""이전 가격제한 체결 알고리즘. 명시적 연구 비교와 기존 기록 복구에만 사용한다."""
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from .models import ZERO, QUANTITY_STEP, FractionalFill, FractionalPosition, aware, decimal


def process_legacy_quote(broker, quote, now: datetime | None = None, *, max_fill_qty: Decimal | None = None,
                         buy_fill_guard=None) -> list[FractionalFill]:
    now = aware(now or quote.received_at)
    broker.roll_session(now)
    broker.expire_orders(now)
    if not broker.quote_is_fresh(quote, now):
        return []
    from .session import SessionExitManager
    schedule = SessionExitManager(broker).schedule(now)
    if schedule is None or not schedule.opens_at <= now < schedule.fractional_deadline:
        return []
    bid, ask = decimal(quote.bid), decimal(quote.ask)
    if quote.symbol in broker.positions:
        broker.positions[quote.symbol].mark_price = bid
    broker.check_daily_loss(now)
    fills = []
    fingerprint = '|'.join(str(v) for v in (aware(quote.timestamp).isoformat(), quote.bid, quote.ask, quote.bid_size, quote.ask_size))
    if broker.quote_watermarks.get(quote.symbol) == fingerprint:
        return fills
    broker.quote_watermarks[quote.symbol] = fingerprint
    liquidity = decimal(max_fill_qty, 'max_fill_qty', nonnegative=True) if max_fill_qty is not None else None
    for order in list(broker.get_open_orders(quote.symbol)):
        if order.status == "unknown":
            continue
        intent = order.intent
        if intent.created_at > aware(quote.timestamp) or intent.created_at > now:
            continue
        side = intent.side
        if side == 'buy':
            from ..session_window import permits_new_entry
            if not permits_new_entry(now,min_entry_to_exit_minutes=broker.settings.min_entry_to_exit_minutes):
                broker.cancel_order(order.client_id,now)
                continue
        price = ask * (1 + broker.settings.slippage_rate) if side == 'buy' else bid * (1 - broker.settings.slippage_rate)
        if (side == 'buy' and price > intent.limit_price) or (side == 'sell' and price < intent.limit_price):
            continue
        if side == 'buy' and (broker.halted or not quote.tradable):
            continue
        remaining = order.remaining_amount / price if side == 'buy' else order.remaining_qty
        available = decimal(quote.ask_size if side == 'buy' else quote.bid_size)
        if liquidity is not None:
            available = min(available, liquidity)
        qty = min(remaining, available).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
        if qty <= 0:
            continue
        notional = qty * price
        fee = broker.settings.fees(side, order.filled_notional + notional) - order.fee
        if side == 'buy' and notional + fee > broker.cash:
            continue
        pnl = ZERO
        if side == 'buy':
            if buy_fill_guard is not None:
                reason = buy_fill_guard(order, quote)
                if reason:
                    order.unfilled_reason = reason
                    continue
            position = broker.positions.get(intent.symbol)
            if position is not None and intent.is_add and price < position.avg_price:
                broker._alert('add_execution_below_average',now,'추가매수 주문 후 평균가 아래에서 모의 체결되었습니다. 위험 예산을 확인하세요.',intent.symbol)
            if position is None:
                position = FractionalPosition(intent.symbol, ZERO, price, ZERO, intent.stop_price, now, intent.strategy)
                broker.positions[intent.symbol] = position
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
            broker.cash -= notional + fee
        else:
            position = broker.positions.get(intent.symbol)
            if position is None or qty > position.qty:
                broker.cancel_order(order.client_id,now)
                continue
            entry_fee = position.entry_fee * qty / position.qty
            pnl = notional - fee - position.avg_price * qty - entry_fee
            broker.cash += notional - fee
            broker.realized_pnl += pnl
            position.qty -= qty
            position.entry_fee -= entry_fee
            if position.qty == 0:
                del broker.positions[intent.symbol]
                broker.exit_intents.pop(intent.symbol, None)
        order.filled_qty += qty
        order.filled_notional += notional
        order.fee += fee
        complete = order.remaining_amount < price * QUANTITY_STEP if side == 'buy' else order.remaining_qty == 0
        order.status = 'filled' if complete else 'partial'
        fill = broker._fill_record(order,qty,price,fee,now,pnl,quote)
        broker.fills.append(fill)
        fills.append(fill)
        if liquidity is not None:
            liquidity -= qty
        broker.check_daily_loss(now)
    return fills

