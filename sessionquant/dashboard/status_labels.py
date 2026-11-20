"""Readable saved reasons, without inferring readiness or changing a trading gate."""
import re

from .labels import LABELS


_CODE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?::[A-Za-z][A-Za-z0-9_]*)?\Z')
_SECRET = re.compile(
    r'password|credential|authorization|bearer|api_?key|client_?secret|'
    r'access_?token|refresh_?token|private_?key|(?:^|_)secret(?:_|$)|'
    r'^(?:sk|ghp|github_pat|ya29)[_-]', re.IGNORECASE)
_CATEGORY_ORDER = {name: index for index, name in enumerate(
    ('운영', '주문·위험', '시세', '분봉', '감시', '거래 자격', '시장', '전략', '기타'))}
_OPERATING = {
    'trading_not_started_or_halted', 'outside_entry_window', 'outside_regular_session',
    'strategy_outside_regular_session', 'OFFLINE', 'STOPPED', 'PREOPEN', 'AFTER_HOURS', 'CLOSED',
}
_RISK = {
    'daily_loss_limit', 'confirmed_company_risk', 'confirmed_news_entry_restriction',
    'macro_reference_risk_restriction', 'preferred_position_target_reached', 'final_candidate_limit',
    'signal_already_consumed', 'duplicate_symbol_signal', 'invalid_evidence', 'invalid_stop',
    'account_order_state_unknown', 'pending_order_or_exit', 'add_conditions_not_met',
    'add_would_average_down', 'invalid_add_stop', 'position_exists', 'existing_risk_limit_breached',
    'position_limit', 'pending_mark_reference_unknown', 'allocation_or_cash_limit',
    'risk_budget_insufficient', 'exit_stop_unverified', 'sector_diversification_unknown',
    'same_sector_limit', 'index_correlation_unknown', 'high_index_correlation_limit',
}


def safe_reason_code(value):
    """Only a short identifier may appear in diagnostic detail, never an error body."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 96 or not _CODE.fullmatch(value) or _SECRET.search(value):
        return None
    return value


def _values(codes):
    if codes is None:
        return []
    if isinstance(codes, str):
        return [item.strip() for item in codes.split(',') if item.strip()]
    return list(codes) if isinstance(codes, (list, tuple)) else [codes]


def is_reason_list(value):
    """Recognize code-only cells; leave prose/evidence translation to its caller."""
    return isinstance(value, (str, list, tuple)) and all(safe_reason_code(v) for v in _values(value))


def _category(code):
    if code in _OPERATING:
        return '운영'
    if code in _RISK:
        return '주문·위험'
    if code.startswith(('quote_', 'candidate_quote_', 'websocket_', 'spread_')):
        return '시세'
    if code.startswith(('universe_', 'watchlist_', 'focus_', 'outside_market_cap_', 'outside_historical_')):
        return '감시'
    if code.startswith(('instrument_', 'paper_', 'eligibility_', 'exchange_')):
        return '거래 자격'
    if code.startswith(('bar_', 'closed_bars_', 'candidate_bar_', 'minute_', 'technical_history_',
                        'quality_', 'new_closed_', 'history_', 'historical_', 'current_session_',
                        'current_closed_', 'rvol_', 'relative_volume_', 'volume_', 'split_volume_',
                        'invalid_or_zero_history_', 'startup_data_')):
        return '분봉'
    if code in {'insufficient_liquidity', 'price_or_turnover_missing'}:
        return '시세'
    if code.startswith(('market_', 'spy_')):
        return '시장'
    if code.startswith(('strategy_', 'simple_', 'technical_signal_', 'trend_')):
        return '전략'
    return '기타'


def blocker_details(codes):
    """Return unique Korean reasons plus safe codes for an optional detail table.

    Unknown identifiers remain distinct in detail. Neither an unknown code nor
    an old saved reason is evidence that the engine is currently disconnected.
    """
    rows, seen = [], set()
    for original in _values(codes):
        code = safe_reason_code(original)
        base = code
        label = LABELS.get(code) if code else None
        if label is None and code and ':' in code:
            symbol, base = code.split(':', 1)
            if symbol in {'SPY', 'QQQ'} and base in LABELS:
                label = symbol+' · '+LABELS[base]
        known = label is not None
        identity = ('label', label) if known else ('code', code)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({'category': _category(base) if known else '기타',
            'label': label if known else '미등록 제한' if code else '사유 형식 확인 필요',
            'code': code, 'known': known})
    return sorted(rows, key=lambda row: _CATEGORY_ORDER[row['category']])


def blocker_summary(codes, *, limit=None, empty='없음'):
    """Compact display text; full safe codes are available only in blocker_details."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('표시할 사유 수는 양의 정수여야 합니다.')
    rows = blocker_details(codes)
    labels = [row['label'] for row in rows if row['known']]
    unknown = sum(not row['known'] for row in rows)
    if unknown:
        labels.append(f'미등록 제한 {unknown}건 · 사유 코드 확인')
    if not labels:
        return empty
    if limit is not None and len(labels) > limit:
        return ', '.join(labels[:limit])+f' 외 {len(labels)-limit}개'
    return ', '.join(labels)


def llm_status_label(value):
    """Label the saved analysis signal, without claiming a request was attempted."""
    labels = {'UNKNOWN': '확인된 분석 없음', 'VERIFIED': '분석 확인',
        'PENDING': '분석 대기', 'REQUESTED': '분석 응답 대기',
        'OUTCOME_UNKNOWN': '분석 요청 결과 미확인', 'NONE': '분석 기록 없음',
        'NO_REQUEST': '분석 요청 없음', 'DISABLED': '분석 사용 안 함',
        'NOT_REQUIRED': '분석 필수 아님', 'IGNORED': '분석 반영 안 함',
        'STALE': '이전 분석 · 갱신 대기', 'EXPIRED': '분석 유효기간 지남',
        'SUCCEEDED': '분석 완료', 'CACHE_HIT': '이전 분석 재사용',
        'FIXTURE': '검증용 가상 분석'}
    return labels.get(value or 'UNKNOWN', '분석 상태 미확인') if isinstance(value, (str, type(None))) else '분석 상태 미확인'
