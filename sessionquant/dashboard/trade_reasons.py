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

ORDER_REASONS = {
    'assumed_order_timeout':'주문 대기 시간 초과',
    'day_expired':'장 마감 · 당일 주문 만료',
    'websocket_quote_unavailable':'실시간 호가 확인 대기',
    'daily_loss_limit':'하루 손실 한도 도달',
    'outside_entry_window':'매수 가능 시간 종료',
    'position_exit_pending':'보유 종목 매도 진행',
    'insufficient_sellable_quantity':'매도 가능한 수량 부족',
    'news_risk_restriction':'뉴스 위험 제한',
    'macro_reference_risk_restriction':'시장 위험 제한',
    'operator_stop_or_data_not_ready':'매매 중지 또는 시작 자료 준비 미완료',
    'awaiting_post_acceptance_quote':'접수 후 새 호가 대기',
    'awaiting_acceptance':'모의 접수 대기',
    'awaiting_quote':'호가 대기',
    'quote_invalid_stale_halted_or_missing_depth':'호가 유효성·최신성·거래 수량 확인 대기',
    'duplicate_or_out_of_order_quote':'새로운 시각의 호가 대기',
    'buy_halted_or_untradable':'매수 중지 또는 거래 조건 확인 대기',
    'liquidity_or_quantity_step_insufficient':'체결 가능한 수량 부족',
    'insufficient_unreserved_cash':'주문에 사용할 잔액 부족',
    'liquidity_partial':'일부 체결 · 남은 수량 대기',
}


def order_cancellation_details(row):
    from .labels import LABELS
    def text(code):
        if not code or code in {'cancelled','cancel_pending'}:
            return '기록 없음'
        return ORDER_REASONS.get(code, LABELS.get(code, '사유 확인 필요'))
    if row.get('status') not in {'cancelled', 'cancel_pending'}:
        return {'취소 사유':'—', '취소 직전 대기 사유':'—'}
    history = row.get('lifecycle') or []
    audit = (history[0].get('order_audit') or {}) if history and isinstance(history[0], dict) else {}
    return {'취소 사유':text(audit.get('cancel_reason')),
            '취소 직전 대기 사유':text(audit.get('cancel_unfilled_reason'))}


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
