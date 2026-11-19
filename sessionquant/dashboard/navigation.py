"""선택한 화면을 URL에 보존해 휴대폰 재연결 후에도 같은 자료를 연다."""

PAGE_TOKENS = {
    '대시보드': 'home', '시장·종목': 'market', '전략·백테스트': 'strategy',
    '매매 내역': 'trades', '뉴스·LLM': 'news', '시스템·설정': 'system',
}
PANEL_TOKENS = {
    '시세·품질': 'quality', '거래량 비교': 'volume', '감시·집중 종목': 'watchlist',
    '매수 판단': 'selection', '시장 참고': 'references', '처리 속도': 'performance', '분석 이력': 'history',
}


def restore_navigation(params, state):
    """URL 변경과 위젯 변경을 구분한다. 새 세션·뒤로 가기도 지원한다."""
    pending = state.pop('_dashboard_navigation', None)
    if isinstance(pending,dict) and pending.get('page') in PAGE_TOKENS:
        params['page'] = PAGE_TOKENS[pending['page']]
        if pending['page'] == '시장·종목':
            params['panel'] = PANEL_TOKENS.get(pending.get('panel'), 'quality')
        elif 'panel' in params:
            del params['panel']
    page_token = params.get('page', 'home')
    pages = {value: key for key, value in PAGE_TOKENS.items()}
    if page_token not in pages:
        page_token = 'home'
    if 'dashboard_page' not in state or state.get('_dashboard_url_page') != page_token:
        state['dashboard_page'] = pages[page_token]
    if page_token == 'market':
        panel_token = params.get('panel', 'quality')
        panels = {value: key for key, value in PANEL_TOKENS.items()}
        if panel_token not in panels:
            panel_token = 'quality'
        if 'market_detail' not in state or state.get('_dashboard_url_panel') != panel_token:
            state['market_detail'] = panels[panel_token]


def remember_navigation(params, state, page, panel=None):
    page_token = PAGE_TOKENS[page]
    if params.get('page') != page_token:
        params['page'] = page_token
    state['_dashboard_url_page'] = page_token
    if page == '시장·종목':
        panel_token = PANEL_TOKENS[panel]
        if params.get('panel') != panel_token:
            params['panel'] = panel_token
        state['_dashboard_url_panel'] = panel_token
    elif 'panel' in params:
        del params['panel']
