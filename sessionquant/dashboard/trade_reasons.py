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

QUOTE_WAIT_REASONS = {
    'feed_not_ready':'실시간 호가 연결·구독 확인 대기',
    'quote_missing':'아직 사용할 수 있는 호가가 없음',
    'quote_conflict':'같은 시각에 서로 다른 호가 수신',
    'quote_time_invalid':'호가 발생·수신 시각 순서 확인 필요',
    'quote_stale':'호가가 허용 시간보다 오래됨',
    'engine_quote_missing':'매매 엔진에 호가 전달 대기',
    'stream_behind_engine':'수신 호가가 엔진 호가보다 오래됨',
    'post_conflict_not_delivered':'충돌 이후 새 호가의 엔진 반영 대기',
    'generation_not_delivered':'재연결·구독 변경 후 호가 반영 대기',
    'engine_quote_conflict':'수신 호가와 엔진 호가 내용 불일치',
    'evidence_unavailable':'상세 원인 기록 실패',
}


def order_quote_wait_details(row):
    from .app import timestamp
    history=row.get('lifecycle') or []
    audit=(history[0].get('order_audit') or {}) if history and isinstance(history[0],dict) else {}
    wait=audit.get('quote_wait') or {}
    evidence=wait.get('evidence') or {}
    reason=evidence.get('reason')
    label=QUOTE_WAIT_REASONS.get(reason, '사유 확인 필요' if reason else '기록 없음')
    if reason=='feed_not_ready':
        label={'DISCONNECTED':'실시간 호가 연결 끊김', 'BACKOFF':'연결 재시도 대기',
               'CONNECTING':'실시간 호가 연결 중', 'CHANGING':'구독 종목 변경 중',
               'STOPPING':'실시간 호가 수신 종료 중', 'STOPPED':'실시간 호가 수신 종료'}.get(
                   evidence.get('connection_state'),label)
    return {'호가 대기 원인':label,
            '마지막 호가 대기 시작':timestamp(wait.get('started_at')),
            '호가 사용 재개':timestamp(wait.get('recovered_at')),
            '수신 호가 발생 시각':timestamp(evidence.get('quote_at')),
            '호가 수신 시각':timestamp(evidence.get('quote_received_at')),
            '엔진 호가 발생 시각':timestamp(evidence.get('engine_quote_at')),
            '호가 경과(초)':evidence.get('quote_age_seconds'),
            '허용 경과(초)':evidence.get('max_age_seconds')}


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
