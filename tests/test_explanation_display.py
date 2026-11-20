from copy import deepcopy

import pytest

from sessionquant.dashboard.explanation_panel import explanation_items, explanation_text, missing_strategy_decision


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


def test_saved_paragraph_is_split_without_cutting_prices_quantities_or_last_sentence():
    text = 'AMD 0.040426주를 494.72423달러에 매수했습니다. 종이 거래 모드의 기록입니다. 끝까지 표시합니다.'
    view = {'sections': {'trades': {'text': text}}}
    assert explanation_items(explanation_text(view, 'trades')) == [
        'AMD 0.040426주를 494.72423달러에 매수했습니다.',
        '모의매매의 기록입니다.', '끝까지 표시합니다.',
    ]
    assert view['sections']['trades']['text'] == text


def test_explicit_news_lines_keep_each_company_and_uncertainty_together():
    text = 'TSLA · 생산 확대가 언급됐습니다. 일정은 미확인입니다.\nMSFT · 새 모델을 발표했습니다. 수익 영향은 미확인입니다.'
    assert explanation_items(text) == text.splitlines()


@pytest.mark.parametrize('status', ['SUCCEEDED', 'BUDGET_BLOCKED'])
def test_home_briefing_renders_complete_long_saved_text_as_escaped_items(status):
    from html import escape
    from streamlit.testing.v1 import AppTest
    text = ('TSLA · 생산량 확대 소식이 있습니다. 구체적인 일정과 수치는 확인되지 않았습니다.\n'
            'MSFT · 새 제품을 발표했습니다. 제공된 기록에는 재무 영향이 명시되어 있지 않습니다.\n'
            'AMZN · 관련 소식이 있습니다. 이 내용만으로 주가가 어떻게 움직일지는 알 수 없습니다.\n'
            'AVGO · 기존 화면에서 잘렸던 마지막 설명도 전부 표시합니다. '
            '이 문장은 화면 표시를 검증하기 위한 합성 자료입니다. <script>untrusted</script> & 원문 끝.')
    assert len(text) > 220
    saved = {'status': 'SUCCEEDED', 'as_of': '2026-11-19T17:00:34+00:00',
             'sections': {'market': {'text': text}, 'strategy': {'text': '반등을 기다립니다.'},
                          'trades': {'text': '모의매매 체결 기록입니다.'}}}
    view = saved if status == 'SUCCEEDED' else {
        'status': status, 'limit_reason': 'HOURLY_CALL_CAP',
        'as_of': '2026-11-19T17:05:34+00:00', 'last_success': saved}
    script = ('from sessionquant.dashboard.explanation_panel import render_explanation_summary\n'
              f'render_explanation_summary({view!r})')
    app = AppTest.from_string(script).run()
    assert not app.exception
    card = next(e.value for e in app.markdown if '<h3>AI 브리핑</h3>' in e.value)
    for line in text.splitlines():
        assert '<li>'+escape(line)+'</li>' in card
    assert '<script>' not in card and '…' not in card
    assert all(label in card for label in ('시장 뉴스', '전략 상태', '최근 모의매매', '11.20 02:00:34 KST'))
    assert card.count('<section') == 3
    if status != 'SUCCEEDED':
        assert any('이전 분석 시점' in c.value for c in app.caption)


@pytest.mark.parametrize('saved_synthetic', [True, False])
def test_detailed_fallback_labels_the_saved_content_source_time_and_provenance(saved_synthetic):
    from streamlit.testing.v1 import AppTest
    saved={'status':'SUCCEEDED','as_of':'2026-11-19T17:00:34+00:00',
           'generated_at':'2026-11-19T17:00:40+00:00','synthetic_inputs':saved_synthetic,
           'sections':{'market':{'text':'이전에 저장한 시장 설명입니다.'}}}
    view={'status':'BUDGET_BLOCKED','limit_reason':'HOURLY_CALL_CAP',
          'as_of':'2026-11-19T17:05:34+00:00','synthetic_inputs':not saved_synthetic,'last_success':saved}
    script=('from sessionquant.dashboard.explanation_panel import render_explanation\n'
            f'render_explanation({view!r})')
    app=AppTest.from_string(script).run()
    assert not app.exception
    source_labels=[row.value for row in app.caption if row.value.startswith('입력 기준 ')]
    assert len(source_labels)==1 and '11.20 02:00:34 KST' in source_labels[0]
    assert '11.20 02:05:34 KST' not in source_labels[0]
    assert any('이전에 저장한 시장 설명' in row.value for row in app.markdown)
    synthetic_warnings=[row.value for row in app.info if '합성 자료에 대한 설명' in row.value]
    assert bool(synthetic_warnings)==saved_synthetic
