"""브라우저 새로고침·뒤로 가기와 화면 클릭을 구분해 선택을 보존한다."""
from pathlib import Path
from contextlib import closing

from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.navigation import restore_navigation, remember_navigation
from sessionquant.database.repository import Repository


def test_navigation_keeps_auth_query_and_handles_back_without_undoing_click():
    params = {'authuser': '1', 'page': 'market', 'panel': 'volume'}
    state = {}
    restore_navigation(params, state)
    assert state['dashboard_page'] == '시장·종목' and state['market_detail'] == '거래량 비교'
    remember_navigation(params, state, state['dashboard_page'], state['market_detail'])
    state['dashboard_page'] = '대시보드'  # 위젯 클릭, URL은 아직 직전 값
    restore_navigation(params, state)
    assert state['dashboard_page'] == '대시보드'
    remember_navigation(params, state, state['dashboard_page'])
    assert params == {'authuser': '1', 'page': 'home'}
    params.update(page='market', panel='volume')  # 브라우저 뒤로 가기
    restore_navigation(params, state)
    assert state['dashboard_page'] == '시장·종목' and state['market_detail'] == '거래량 비교'


def test_unrecognized_url_values_use_known_screen():
    state = {}
    restore_navigation({'page': '../../other', 'panel': 'arbitrary'}, state)
    assert state['dashboard_page'] == '대시보드'
    restore_navigation({'page': 'market', 'panel': 'arbitrary'}, state)
    assert state['market_detail'] == '시세·품질'


def test_new_browser_session_restores_same_market_table_without_issuing_orders(tmp_path, monkeypatch):
    path = tmp_path/'navigation.db'
    repo = Repository(path, backend='sqlite')
    repo.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL'})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    entry = str(Path('sessionquant/dashboard/session_app.py').resolve())
    first = AppTest.from_file(entry, default_timeout=30).run()
    first.radio(key='dashboard_page').set_value('시장·종목').run()
    first.radio(key='market_detail').set_value('거래량 비교').run()
    assert not first.exception
    # 새로고침으로 기존 session_state가 사라져도 URL만으로 화면을 복원한다.
    second = AppTest.from_file(entry, default_timeout=30)
    second.query_params.update(first.query_params)
    second.run()
    assert not second.exception
    assert second.radio(key='dashboard_page').value == '시장·종목'
    assert second.radio(key='market_detail').value == '거래량 비교'
    assert any(row.value == '감시목록 과거 분봉·RVOL' for row in second.subheader)
    second.radio(key='dashboard_page').set_value('대시보드').run()
    assert not second.exception and second.radio(key='dashboard_page').value == '대시보드'
    repo = Repository(path, backend='sqlite')
    try:
        assert not repo.all('order_intents') and not repo.all('orders')
    finally:
        repo.close()


def test_home_shortcuts_open_the_requested_page_and_table_without_orders(tmp_path,monkeypatch):
    path=tmp_path/'shortcuts.db'
    with closing(Repository(path,backend='sqlite')) as repo:
        repo.set_control('sessionquant_context',{'environment':'PAPER_LOCAL'})
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    monkeypatch.setattr('sys.argv',['session_app.py','--db',str(path)])
    app=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30)
    app.query_params['authuser']='1'
    app.run().button(key='go-매매 전체 보기').click().run()
    assert not app.exception and app.radio(key='dashboard_page').value=='매매 내역'
    assert app.query_params['page']=='trades' and app.query_params['authuser']=='1'
    app.radio(key='dashboard_page').set_value('대시보드').run()
    app.button(key='go-종목과 대기 이유 보기').click().run()
    assert not app.exception and app.radio(key='dashboard_page').value=='시장·종목'
    assert app.radio(key='market_detail').value=='매수 판단'
    assert app.query_params['panel']=='selection'
    app.radio(key='dashboard_page').set_value('대시보드').run()
    app.button(key='go-뉴스·AI 자세히 보기').click().run()
    assert not app.exception and app.radio(key='dashboard_page').value=='뉴스·LLM'
    assert app.query_params['page']=='news' and app.query_params['authuser']=='1'
    assert 'panel' not in app.query_params
    restored=AppTest.from_file(str(Path('sessionquant/dashboard/session_app.py').resolve()),default_timeout=30)
    restored.query_params.update(app.query_params)
    restored.run()
    assert not restored.exception and restored.radio(key='dashboard_page').value=='뉴스·LLM'
    with closing(Repository(path,backend='sqlite')) as repo:
        assert not repo.all('orders') and not repo.all('order_intents')
