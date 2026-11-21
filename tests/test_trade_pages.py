from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.trade_cards import trade_cards_html


def test_history_pages_default_all_period_cap_and_filter_reset():
    app = AppTest.from_string('''
from datetime import datetime, timezone, timedelta
import streamlit as st
from sessionquant.dashboard.session_app import DashboardData
from sessionquant.dashboard.overview import filter_trades
from sessionquant.dashboard.trade_pages import trade_page
from sessionquant.dashboard.trade_cards import trade_cards_html
x = DashboardData()
x.tables['executions'] = [{'id': str(i), 'symbol': 'AMD' if i % 2 else 'AAPL',
    'side': 'buy', 'qty': '0.04', 'price': '500',
    'timestamp': (datetime.now(timezone.utc) - timedelta(days=3, minutes=80-i)).isoformat()}
    for i in range(80)]
counts = filter_trades(x)
page = trade_page(x.tables['executions'], kind='executions', source_count=counts['executions'])
st.json([r['id'] for r in page])
st.markdown(trade_cards_html(page), unsafe_allow_html=True)
st.session_state['export_count'] = len(x.tables['executions'])
''', default_timeout=20).run()
    assert not app.exception
    assert app.selectbox(key='trade_period_v2').value == 0
    assert app.session_state['export_count'] == 80
    import json
    assert json.loads(app.json[0].value) == [str(i) for i in range(79,64,-1)]
    app.selectbox(key='trade_detail_executions_page').set_value(5).run()
    assert json.loads(app.json[0].value) == [str(i) for i in range(19,4,-1)]
    assert app.selectbox(key='trade_detail_executions_page').options == [f'{i} / 5 페이지' for i in range(1,6)]
    app.text_input(key='trade_symbol').set_value('AMD').run()
    assert app.selectbox(key='trade_detail_executions_page').value == 1
    assert len(json.loads(app.json[0].value)) == 15
    app.selectbox(key='trade_period_v2').set_value(1).run()
    assert not app.exception
    assert any('선택한 기간' in item.value for item in app.info)
    assert app.session_state['export_count'] == 0


def test_cards_show_side_amount_precision_reason_and_escape():
    html = trade_cards_html([{'symbol':'AMD', 'side':'sell', 'qty':'0.040426',
        'price':'500', 'timestamp':'2026-11-19T16:15:14Z', '매도 이유':'손절 기준 가격 도달'},
        {'symbol':'<script>x</script>', 'side':'buy', 'qty':'1', 'price':'20'}])
    assert '매도' in html and '매수' in html and '에이엠디' in html
    assert '$20.21' in html and '0.040426주' in html
    assert '11.20 01:15:14 KST' in html and '손절 기준 가격 도달' in html
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_home_page_is_independent_of_detail_filters_and_page():
    app = AppTest.from_string('''
import streamlit as st
from sessionquant.dashboard.trade_pages import trade_page
st.session_state['trade_period_v2'] = 1
st.session_state['trade_symbol'] = 'MSFT'
rows = [{'id': i, 'timestamp': '2026-11-19T16:15:14Z'} for i in range(31)]
st.json([r['id'] for r in trade_page(rows,kind='executions',source_count=31,scope='home')])
''').run()
    assert not app.exception
    app.selectbox(key='trade_home_executions_page').set_value(3).run()
    assert app.json[0].value == '[30]'


def test_home_loads_older_sale_reasons_for_cards(tmp_path, monkeypatch):
    from sessionquant.database.repository import Repository
    from sessionquant.dashboard.session_app import load_database
    from sessionquant.dashboard.trade_reasons import execution_reasons
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    path = tmp_path / 'history.db'
    repo = Repository(path, backend='sqlite')
    for i, symbol in enumerate(('AMD', 'AAPL')):
        repo.import_record('orders', {'client_id':f'o{i}', 'symbol':symbol, 'side':'sell',
                                     'reason':'stop_loss'}, f'o{i}')
        repo.import_record('executions', {'id':f'f{i}', 'client_id':f'o{i}', 'symbol':symbol,
            'side':'sell', 'qty':'0.04', 'price':'500', 'timestamp':f'2026-11-19T16:5{i}:00Z'}, f'f{i}')
    repo.close()
    data = load_database(path, page='대시보드', panel='live')
    assert not data.error and len(data.tables['executions']) == 2
    rows = execution_reasons(data.tables['executions'], orders=data.tables['orders'], state=data.state)
    assert [r['매도 이유'] for r in rows] == ['손절 기준 가격 도달'] * 2
