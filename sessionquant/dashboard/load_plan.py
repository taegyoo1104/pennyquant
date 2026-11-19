"""화면에서 실제로 사용하는 기록만 읽는다. 원본 분봉은 화면 조회 대상이 아니다."""

CONTROLS = {
    'context': ('sessionquant_context', '실행 정보'),
    'engine_state': ('trade_control_state', '엔진 상태'),
    'actual_account': ('actual_account_snapshot', '실제 계좌'),
    'account_status': ('account_monitor_status', '계좌 조회 상태'),
    'watchlist_state': ('watchlist_dashboard_v1', '감시 목록'),
    'market_quality': ('market_data_quality_v1', '시세 품질'),
    'market_performance': ('market_performance_v1', '처리 시간'),
    'watchlist_rvol': ('watchlist_rvol_v1', '거래량 비교'),
    'history_backfill': ('watchlist_history_backfill_v1', '분봉 준비'),
    'portfolio_selection': ('portfolio_selection_v1', '매수 후보 선정'),
    'market_reference': ('market_reference_view_v1', '시장 참고 지표'),
    'news_status': ('session_news_status', '뉴스 수집 상태'),
    'warmup': ('startup_data_warmup_v1', '자료 준비'),
    'strategy_explanation': ('strategy_explanation_v1', 'AI 전략 설명'),
}

PAGE_CONTROLS = {
    '대시보드': ('actual_account', 'account_status', 'watchlist_state', 'warmup', 'news_status', 'strategy_explanation'),
    '시장·종목': ('watchlist_state', 'market_quality', 'market_performance', 'watchlist_rvol',
                 'history_backfill', 'portfolio_selection', 'market_reference'),
    '전략·백테스트': (),
    '매매 내역': ('actual_account', 'account_status'),
    '뉴스·LLM': ('actual_account', 'account_status', 'watchlist_state', 'news_status', 'strategy_explanation'),
    '시스템·설정': ('warmup', 'market_reference', 'news_status'),
}

# 최신 상태 하나와 이력 표를 구분한다. 체결 내보내기·뉴스 집계의 기존 범위는 유지한다.
PAGE_TABLES = {
    '대시보드': dict.fromkeys(('executions', 'news_events', 'news_analysis', 'quote_snapshots', 'news_ingestions'), 300),
    '시장·종목': {'market_regimes': 1, 'quote_snapshots': 300, 'candidates': 300, 'economic_events': 200},
    '전략·백테스트': {'account_snapshots': 2000, 'daily_performance': 2000},
    '매매 내역': {'session_state': 1, 'positions': 2000, 'account_snapshots': 1, 'quote_snapshots': 300,
                 'orders': 2000, 'executions': 2000, 'alerts': 300},
    '뉴스·LLM': dict.fromkeys(('news_events', 'news_analysis', 'macro_news', 'macro_analysis',
                             'news_ingestions', 'news_enrichments'), 2000),
    '시스템·설정': {'alerts': 300, 'pipeline_status': 100},
}

SOURCE_LABELS = {
    **{name: label for name, (_, label) in CONTROLS.items()},
    'connection': 'DB 연결', 'state': '전략 장부', 'broker_status': '거래 모드',
    'market_regimes': '시장 방향', 'quote_snapshots': '최근 호가', 'candidates': '기술 분석 이력',
    'economic_events': '경제 일정', 'fred_references': '일별 참고 지표',
    'session_state': '청산 상태', 'positions': '보유 종목', 'account_snapshots': '계좌 이력',
    'orders': '주문', 'executions': '체결', 'alerts': '운영 경고', 'daily_performance': '전략 성과',
    'pipeline_status': '처리 이력', 'news_events': '기업 뉴스', 'news_analysis': '기업 뉴스 분석',
    'macro_news': '시장 뉴스', 'macro_analysis': '시장 뉴스 분석', 'news_ingestions': '뉴스 수신',
    'news_enrichments': '뉴스 원문 확인', 'news_schedule': '뉴스 수집 일정',
    'news_flow': '뉴스·AI 처리 요약', 'transaction': 'DB 조회 마무리',
}


NEWS_SECTIONS = {
    '기업 뉴스': ('news_events', 'news_analysis'),
    '경제·정치·국제정세': ('macro_news', 'macro_analysis'),
    'LLM 사용량': (),
}


# 펼쳐 놓지 않은 화면의 큰 상태·이력을 자동 갱신 때 함께 읽지 않는다.
MARKET_SECTIONS = {
    '시세·품질': (('market_quality',), {}),
    '거래량 비교': (('watchlist_rvol', 'history_backfill'), {}),
    '감시·집중 종목': (('watchlist_state',), {'candidates': 300}),
    '매수 판단': (('portfolio_selection',), {}),
    '시장 참고': (('market_reference',), {'market_regimes': 1, 'quote_snapshots': 300, 'economic_events': 200}),
    '처리 속도': (('market_performance', 'market_quality'), {}),
    '분석 이력': ((), {'candidates': 300}),
}


def read_plan(page, *, news_section=None, panel=None):
    if panel is not None:
        if page == '대시보드' and panel == 'live':
            return ('context', 'engine_state', 'actual_account', 'account_status', 'watchlist_state', 'warmup'), {
                'executions': 1, 'quote_snapshots': 300}
        if page == '대시보드' and panel == 'news':
            return ('context', 'engine_state', 'news_status', 'strategy_explanation'), {'news_ingestions': 300}
        if page == '시장·종목' and panel in MARKET_SECTIONS:
            controls, tables = MARKET_SECTIONS[panel]
            return ('context', *controls), tables
        raise ValueError('지원하지 않는 대시보드 구역입니다.')
    if page is None:
        tables = {}
        for plan in PAGE_TABLES.values():
            for name, limit in plan.items():
                tables[name] = max(limit, tables.get(name, 0))
        return tuple(CONTROLS), tables
    if page not in PAGE_TABLES:
        raise ValueError('지원하지 않는 대시보드 화면입니다.')
    tables = PAGE_TABLES[page]
    if page == '뉴스·LLM' and news_section is not None:
        if news_section not in NEWS_SECTIONS:
            raise ValueError('지원하지 않는 뉴스 화면입니다.')
        needed = ('news_ingestions', 'news_enrichments', *NEWS_SECTIONS[news_section])
        tables = {name: limit for name, limit in tables.items() if name in needed}
    controls = dict.fromkeys(('context', 'engine_state', 'actual_account', 'account_status', *PAGE_CONTROLS[page]))
    return tuple(controls), tables
