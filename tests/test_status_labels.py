"""Saved blocker codes should remain understandable in the rendered/exported table."""
import ast
from copy import deepcopy
import inspect
import json

import pytest

from sessionquant.dashboard.labels import LABELS, display_rows
from sessionquant.dashboard.status_labels import blocker_details, blocker_summary, safe_reason_code, llm_status_label
from sessionquant.market.scanner import scan


def test_every_static_scanner_reason_has_a_readable_label():
    tree = ast.parse(inspect.getsource(scan))
    codes = {node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'append' and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)}
    codes.update('instrument_'+field+'_unverified_or_restricted'
                 for field in ('tradable', 'fractional', 'halted', 'corporate_action'))
    assert codes and codes <= LABELS.keys()
    assert all(row['known'] for row in blocker_details(sorted(codes)))


def test_preopen_shaped_blockers_export_with_specific_deduplicated_reasons():
    # The uploaded CSV had already lost its raw codes. This uses actual engine
    # code names, rather than claiming to reconstruct those particular rows.
    codes = ['closed_bars_missing_or_stale', 'technical_history_incomplete',
        'insufficient_liquidity', 'minute_history_incomplete_or_stale',
        'candidate_bar_time_mismatch', 'technical_signal_absent',
        'websocket_quote_unavailable', 'trading_not_started_or_halted',
        'technical_history_incomplete']
    original = [{'종목': 'GOOG', '차단 사유': ', '.join(codes), '주문 판단': 'BLOCKED'}]
    before = deepcopy(original)
    visible, unknown = display_rows(original)
    assert not unknown and original == before
    assert '상세 확인 필요' not in visible[0]['차단 사유']
    assert '실시간 호가 확인 전 매수 대기' in visible[0]['차단 사유']
    assert visible[0]['차단 사유'].count('매매 지표를 계산할 당일 분봉 부족') == 1
    assert visible[0]['차단 사유'].startswith('매매 시작 대기·중지')
    assert '개장 전' not in visible[0]['차단 사유']  # Missing input alone cannot prove the session phase.
    assert {row['category'] for row in blocker_details(codes)} >= {'운영', '시세', '분봉', '전략'}


def test_unknown_codes_stay_distinct_in_detail_without_repeating_generic_summary():
    codes = ['quote_stale', 'new_provider_rule', 'new_provider_rule', 'new_feed_rule']
    summary = blocker_summary(codes)
    assert summary == '시세 갱신 지연, 미등록 제한 2건 · 사유 코드 확인'
    assert 'new_' not in summary
    details = blocker_details(codes)
    assert [row['code'] for row in details if not row['known']] == ['new_provider_rule', 'new_feed_rule']
    display, unknown = display_rows([{'차단 사유': summary}])
    assert display[0]['차단 사유'] == summary and not unknown
    detail_rows, _ = display_rows([{'구분': row['category'], '사유': row['label'],
        '사유 코드': row['code']} for row in details])
    assert detail_rows[1]['사유 코드'] == 'new_provider_rule'
    raw, unknown = display_rows([{'차단 사유': ', '.join(codes)}])
    assert raw[0]['차단 사유'] == summary and unknown


@pytest.mark.parametrize('payload', [
    'https://example.invalid/?api_key=PRIVATE', 'Authorization: Bearer PRIVATE',
    'password_PRIVATE', 'sk_project_PRIVATE', 'client_secret_PRIVATE',
    'new_rule\nsecret=PRIVATE', 'x'*97, {'token': 'PRIVATE'},
])
def test_unknown_details_do_not_echo_messages_urls_or_credentials(payload):
    assert safe_reason_code(payload) is None
    detail = blocker_details([payload])[0]
    assert detail == {'category': '기타', 'label': '사유 형식 확인 필요', 'code': None, 'known': False}
    assert 'PRIVATE' not in json.dumps(detail)
    visible, _ = display_rows([{'사유 코드': payload}])
    assert visible[0]['사유 코드'] == '원문 표시 제외'


def test_summary_priority_limit_and_identical_labels_are_stable():
    codes = ['simple_rebound_missing', 'quote_stale', 'daily_loss_limit',
        'watchlist_history_synthetic_blocked', 'watchlist_synthetic_blocked']
    details = blocker_details(codes)
    assert len(details) == 4
    assert details[0]['label'] == '하루 손실 한도 도달'
    assert blocker_summary(codes, limit=1) == '하루 손실 한도 도달 외 3개'
    assert blocker_summary([], empty='판단 기록 없음') == '판단 기록 없음'
    assert blocker_summary('SPY:quote_unknown_or_stale') == 'SPY · 호가 미수신·갱신 지연'
    for invalid in (0, -1, True):
        with pytest.raises(ValueError):
            blocker_summary(codes, limit=invalid)


def test_missing_stale_preopen_offline_and_subscription_are_not_equated():
    rows, unknown = display_rows([{'상태': state} for state in
        ('MISSING', 'STALE', 'PREOPEN', 'OFFLINE', 'SUBSCRIBED', 'DISCONNECTED')])
    assert not unknown
    assert [row['상태'] for row in rows] == [
        '미수신', '오래된 자료', '개장 전', '프로그램 종료', '실시간 시세 구독 확인', '실시간 시세 연결 끊김']
    assert '연결 끊김' not in blocker_summary(['websocket_quote_unavailable'])
    assert blocker_summary(['outside_entry_window']) == '매수 가능 시간 아님'


def test_simple_pullback_stages_and_saved_order_llm_states_are_readable():
    rows, unknown = display_rows([{'전략 진행': 'CORRECTION', '주문 판단': 'APPROVED',
        '분석 상태': 'NOT_REQUIRED', '대기·차단 이유': ['simple_rebound_missing']},
        {'전략 진행': 'DATA_WAIT', '주문 판단': 'ALLOCATION_REJECTED', '분석 상태': 'PENDING'}])
    assert not unknown
    assert rows[0] == {'전략 진행': '조정 중 · 반등 대기', '주문 판단': '주문 승인',
        '분석 상태': '필수 아님', '대기·차단 이유': '직전 고가 위 확정봉 반등 대기'}
    assert rows[1]['분석 상태'] == '처리 대기'


def test_llm_signal_status_does_not_confuse_unverified_analysis_with_a_pending_request():
    assert llm_status_label('UNKNOWN') == '확인된 분석 없음'
    assert llm_status_label(None) == '확인된 분석 없음'
    assert llm_status_label('PENDING') == '분석 대기'
    assert llm_status_label('VERIFIED') == '분석 확인'
    assert llm_status_label('OUTCOME_UNKNOWN') == '분석 요청 결과 미확인'
    assert llm_status_label('NOT_REQUIRED') == '분석 필수 아님'
    assert llm_status_label('provider_error_with_secret') == '분석 상태 미확인'
    visible, unknown = display_rows([{'LLM 분석': llm_status_label('UNKNOWN')}])
    assert visible == [{'LLM 분석': '확인된 분석 없음'}] and not unknown
