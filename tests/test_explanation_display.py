from copy import deepcopy

import pytest

from sessionquant.dashboard.explanation_panel import explanation_text, missing_strategy_decision


@pytest.mark.parametrize('code,name', [
    ('pullback_simple', '단순 눌림목'), ('pullback', '눌림목'),
    ('momentum', '상승 추세'), ('gap_breakout', '갭 돌파'),
    ('vwap', '평균 거래가격 회복'),
])
def test_saved_narration_translates_complete_codes_without_changing_symbols_or_original(code, name):
    view = {'sections': {'strategy': {'text': f"AAPL·LOW의 '{code}' 전략을 확인합니다. my_{code}는 원문입니다."}}}
    original = deepcopy(view)
    assert explanation_text(view, 'strategy') == f"AAPL·LOW의 '{name}' 전략을 확인합니다. my_{code}는 원문입니다."
    assert view == original


def test_no_decisions_does_not_present_news_as_an_unproven_entry_blocker():
    original_text = "현재 전략 'pullback_simple'에 대해 기록된 결정이 없으며, 뉴스의 부정적 요소로 인해 매매 신호가 발생하지 않아 대기 중입니다."
    view = {
        'facts': {'strategy': [{'id': 'strategy:none', 'status': 'NO_RECORDED_DECISION'}]},
        'sections': {'strategy': {'text': original_text}, 'market': {'text': '뉴스에 부정적인 내용이 있습니다.'}},
    }
    original = deepcopy(view)
    assert missing_strategy_decision(view)
    assert '기록된 전략 판단이 없습니다' in explanation_text(view, 'strategy')
    assert '매수 대기 이유를 알 수 없습니다' in explanation_text(view, 'strategy')
    assert '뉴스' not in explanation_text(view, 'strategy')
    assert explanation_text(view, 'market') == original['sections']['market']['text']
    assert view == original


@pytest.mark.parametrize('facts', [None, {}, {'strategy': []},
    {'strategy': [{'id': 'decision:AAPL', 'blockers': ['simple_rebound_missing']}]},
    {'strategy': [{'id': 'strategy:none', 'status': 'UNKNOWN'}]},
])
def test_missing_or_real_facts_do_not_get_replaced_with_an_empty_decision_claim(facts):
    view = {'facts': facts, 'sections': {'strategy': {'text': 'pullback_simple은 반등 조건을 기다립니다.'}}}
    assert not missing_strategy_decision(view)
    assert explanation_text(view, 'strategy') == '단순 눌림목은 반등 조건을 기다립니다.'
