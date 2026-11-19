"""Read the recorded cause of a sale; never infer it from losses or later signals."""

SELL_REASONS = {
    'stop_loss':'손절 기준 가격 도달',
    'net_profit_target':'목표 수익 도달',
    'profit_protection':'수익 보호 · 하락 신호 확인',
    'daily_loss_limit':'하루 손실 한도 도달',
    'holding_time_exit':'정해진 보유 시간 종료',
    'session_exit':'장 마감 전 정리',
    'overnight_position':'전 거래일 잔여 보유 정리',
    'confirmed_adverse_news':'확인된 악재',
    'manual_market_sell':'직접 전량 매도',
    'operator_stop_all':'매매 종료 · 전체 매도',
}


def execution_reasons(rows, *, orders=(), state=None):
    """An older fill may use its exact order ID, symbol and side from this ledger."""
    by_order = {}
    stored = ((state or {}).get('broker') or {}).get('orders') or {}
    for order in [*orders, *(stored.values() if isinstance(stored,dict) else [])]:
        intent = order.get('intent') or order
        key = (intent.get('client_id'), intent.get('symbol'), str(intent.get('side')).lower())
        reason = intent.get('reason')
        if key[0] and isinstance(reason,str) and reason.strip():
            by_order.setdefault(key,set()).add(reason.strip())
    result = []
    for row in rows:
        code, text = None, '—'
        if str(row.get('side')).lower() == 'sell':
            own = row.get('reason')
            if isinstance(own,str) and own.strip():
                code = own.strip()
            else:
                matches = by_order.get((row.get('client_id'), row.get('symbol'), 'sell'), set())
                if len(matches) == 1:
                    code = next(iter(matches))
            text = SELL_REASONS.get(code, '기타 사유 · 주문 기록 확인' if code else '기록 없음')
        result.append({**row, 'sell_reason_code':code, '매도 이유':text})
    return result
